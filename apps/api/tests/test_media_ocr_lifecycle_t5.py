import asyncio
import gzip
import os
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO

import httpx
import pytest
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.db.models import Author, SocialAccount, SocialMediaAsset, SocialPost, SocialPostProcessing
from app.modules.social.collector import CollectorPost
from app.modules.social.media_archive import SocialAvatarArchive, SocialMediaArchive
from app.modules.social.media_io import AsyncConcurrencyGate
from app.modules.social.ocr import LocalMediaOcr, OcrResult
from app.modules.social.service import SocialSyncService


def _make_image_bytes(
    mode: str,
    size: tuple[int, int],
    color: object,
    fmt: str,
    *,
    exif_orientation: int | None = None,
    transparency: int | None = None,
) -> bytes:
    img = Image.new(mode, size, color)
    buf = BytesIO()
    kwargs: dict[str, object] = {"format": fmt}
    if exif_orientation is not None:
        exif = img.getexif()
        exif[0x0112] = exif_orientation
        kwargs["exif"] = exif
    if transparency is not None:
        kwargs["transparency"] = transparency
    img.save(buf, **kwargs)
    return buf.getvalue()


class _ChunkedStream(httpx.AsyncByteStream):
    def __init__(
        self,
        chunks: list[bytes],
        *,
        on_yield: list[int] | None = None,
        delay_seconds: float = 0.0,
    ) -> None:
        self._chunks = chunks
        self._on_yield = on_yield
        self._delay_seconds = delay_seconds

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for idx, chunk in enumerate(self._chunks):
            if self._delay_seconds > 0:
                await asyncio.sleep(self._delay_seconds)
            if self._on_yield is not None:
                self._on_yield.append(idx)
            yield chunk


@pytest.mark.asyncio
async def test_streaming_limit_aborts_on_exceeding_chunk_and_handles_headers(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.modules.social import media_archive

    monkeypatch.setattr(media_archive, "MAX_SOURCE_BYTES", 1024)
    monkeypatch.setattr(media_archive, "MAX_AVATAR_BYTES", 512)

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    chunk_log_no_cl: list[int] = []
    chunk_log_lying_cl: list[int] = []
    chunk_log_bad_cl: list[int] = []
    chunk_log_big_cl: list[int] = []

    raw_big = b"x" * 2000
    gz_big = gzip.compress(raw_big)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/media/no-cl":
            return httpx.Response(
                200,
                stream=_ChunkedStream(
                    [b"a" * 600, b"b" * 600, b"c" * 600],
                    on_yield=chunk_log_no_cl,
                ),
            )
        if path == "/media/lying-cl":
            return httpx.Response(
                200,
                headers={"Content-Length": "100"},
                stream=_ChunkedStream(
                    [b"a" * 500, b"b" * 700, b"c" * 700], on_yield=chunk_log_lying_cl
                ),
            )
        if path == "/media/bad-cl":
            return httpx.Response(
                200,
                headers={"Content-Length": "not-a-number"},
                stream=_ChunkedStream([b"a" * 700, b"b" * 700], on_yield=chunk_log_bad_cl),
            )
        if path == "/media/big-cl":
            return httpx.Response(
                200,
                headers={"Content-Length": "999999"},
                stream=_ChunkedStream([b"a" * 200], on_yield=chunk_log_big_cl),
            )
        if path == "/media/gzip-bomb":
            # Compressed size < 1024, decompressed size > 1024
            return httpx.Response(
                200,
                headers={"Content-Encoding": "gzip", "Content-Length": str(len(gz_big))},
                content=gz_big,
            )
        if path == "/avatar/too-big":
            return httpx.Response(
                200,
                stream=_ChunkedStream([b"a" * 300, b"b" * 300, b"c" * 300]),
            )
        return httpx.Response(404)

    settings = Settings(social_media_dir=tmp_path, _env_file=None)
    with Session(engine) as session:
        author = Author(name="Author")
        session.add(author)
        session.flush()
        account = SocialAccount(author_id=author.id, handle="creator", status="confirmed")
        session.add(account)
        session.flush()
        post = SocialPost(
            account_id=account.id,
            platform_post_id="1",
            url="https://x.com/creator/status/1",
            content_hash="h1",
            posted_at=datetime.now(UTC),
        )
        session.add(post)
        session.flush()

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            archive = SocialMediaArchive(session, settings, client)
            for idx, suffix in enumerate(
                ["no-cl", "lying-cl", "bad-cl", "big-cl", "gzip-bomb"]
            ):
                asset = await archive.archive(
                    post.id, idx, f"https://pbs.twimg.com/media/{suffix}"
                )
                assert asset.status == "failed"
                assert "上限" in (asset.error or "")

            avatar_archive = SocialAvatarArchive(settings, client)
            assert (
                await avatar_archive.archive("https://pbs.twimg.com/avatar/too-big")
            ) is None

    # Aborted on chunk 1 (index 1, 600+600=1200 > 1024) without consuming chunk 2.
    assert chunk_log_no_cl == [0, 1]
    assert chunk_log_lying_cl == [0, 1]
    assert chunk_log_bad_cl == [0, 1]
    # Rejected immediately from Content-Length hint without reading any body chunks.
    assert chunk_log_big_cl == []


@pytest.mark.asyncio
async def test_ocr_remote_fetch_enforces_configurable_byte_and_pixel_limits(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.modules.social.ocr.shutil.which", lambda _: "/usr/bin/tesseract")
    giant_png = _make_image_bytes("RGB", (4000, 4000), (10, 20, 30), "PNG")
    # Small compressed PNG (< 100 KB) but 16 MP > configured 1 MP limit.
    assert len(giant_png) < 100_000

    valid_jpg = _make_image_bytes("RGB", (400, 400), (200, 200, 200), "JPEG")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/media/over-bytes.jpg":
            return httpx.Response(200, content=b"z" * 150_000)
        if request.url.path == "/media/giant-pixels.png":
            return httpx.Response(200, content=giant_png)
        if request.url.path == "/media/ok.jpg":
            return httpx.Response(200, content=valid_jpg)
        return httpx.Response(404)

    settings = Settings(
        social_media_dir=tmp_path,
        social_ocr_enabled=True,
        social_ocr_max_image_bytes=100_000,
        social_ocr_max_image_pixels=1_000_000,
        _env_file=None,
    )
    recognized: list[str] = []

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        ocr = LocalMediaOcr(settings, client=client)

        async def fake_recognize(path: str) -> bytes | None:
            recognized.append(path)
            return b"recognized text"

        monkeypatch.setattr(ocr, "_recognize", fake_recognize)

        res = await ocr.extract_result(
            [
                {"type": "image", "url": "https://pbs.twimg.com/media/over-bytes.jpg"},
                {"type": "image", "url": "https://pbs.twimg.com/media/giant-pixels.png"},
                {"type": "image", "url": "https://pbs.twimg.com/media/ok.jpg"},
            ]
        )

    assert res.status == "retry"
    assert res.text == "recognized text"
    assert len(recognized) == 1
    assert list(tmp_path.glob("mangafinder-ocr-*")) == []


@pytest.mark.asyncio
async def test_image_formats_exif_alpha_corrupt_and_pixel_limit(tmp_path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    valid_jpg = _make_image_bytes("RGB", (1800, 1200), (80, 100, 120), "JPEG")
    rotated_jpg = _make_image_bytes(
        "RGB", (1200, 800), (200, 120, 40), "JPEG", exif_orientation=6
    )
    rgba_png = _make_image_bytes("RGBA", (900, 900), (10, 200, 100, 128), "PNG")
    palette_trans_png = _make_image_bytes(
        "RGBA", (600, 600), (10, 200, 100, 0), "PNG"
    )
    img_p = Image.open(BytesIO(palette_trans_png)).convert("P")
    p_buf = BytesIO()
    img_p.save(p_buf, format="PNG", transparency=0)
    p_trans_bytes = p_buf.getvalue()

    trunc_jpg = valid_jpg[: len(valid_jpg) // 2]
    corrupt_bytes = b"not-a-valid-image"
    over_pixel_png = _make_image_bytes("RGB", (2000, 2000), (50, 50, 50), "PNG")

    payloads = {
        "/media/valid.jpg": valid_jpg,
        "/media/rotated.jpg": rotated_jpg,
        "/media/rgba.png": rgba_png,
        "/media/palette.png": p_trans_bytes,
        "/media/trunc.jpg": trunc_jpg,
        "/media/corrupt.jpg": corrupt_bytes,
        "/media/overpixel.png": over_pixel_png,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in payloads:
            return httpx.Response(200, content=payloads[request.url.path])
        return httpx.Response(404)

    settings = Settings(
        social_media_dir=tmp_path,
        social_media_max_dimension=800,
        social_media_max_pixels=3_000_000,  # 2000x2000 = 4M exceeds, 1800x1200 = 2.16M allowed
        social_media_cache_max_bytes=1024**3,
        _env_file=None,
    )

    with Session(engine) as session:
        author = Author(name="Author")
        session.add(author)
        session.flush()
        account = SocialAccount(author_id=author.id, handle="creator", status="confirmed")
        session.add(account)
        session.flush()
        post = SocialPost(
            account_id=account.id,
            platform_post_id="2",
            url="https://x.com/creator/status/2",
            content_hash="h2",
            posted_at=datetime.now(UTC),
            media=[
                {"type": "image", "url": f"https://pbs.twimg.com{path}"}
                for path in payloads
            ],
        )
        session.add(post)
        session.flush()

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            paths = await SocialMediaArchive(session, settings, client).archive_post(post)

        assert set(paths.keys()) == {0, 1, 2, 3}
        assert Image.open(paths[0]).size == (800, 533)
        # EXIF orientation 6 (1200x800 rotated 90 deg -> 533x800)
        assert Image.open(paths[1]).size == (533, 800)
        # Alpha preserved for RGBA and palette transparency PNGs
        assert Image.open(paths[2]).mode == "RGBA"
        assert Image.open(paths[3]).mode == "RGBA"

        assets = {
            a.media_index: a
            for a in session.query(SocialMediaAsset).filter_by(post_id=post.id).all()
        }
        assert assets[4].status == "failed"
        assert assets[5].status == "failed"
        assert assets[6].status == "failed"
        assert "像素" in (assets[6].error or "")
        assert list((tmp_path / "archive").glob(".tmp-*")) == []


@pytest.mark.asyncio
async def test_cancellation_cleans_temp_files_and_keeps_existing_cache(tmp_path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    valid_jpg = _make_image_bytes("RGB", (600, 600), (10, 20, 30), "JPEG")

    def slow_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/media/slow.jpg":
            return httpx.Response(
                200,
                stream=_ChunkedStream([valid_jpg[:200], valid_jpg[200:]], delay_seconds=0.5),
            )
        if request.url.path == "/avatar/existing.jpg":
            return httpx.Response(200, content=b"corrupt")
        return httpx.Response(200, content=valid_jpg)

    settings = Settings(social_media_dir=tmp_path, _env_file=None)
    # Pre-seed an existing valid avatar file; failed archive must not overwrite it.
    avatar_url = "https://pbs.twimg.com/avatar/existing.jpg"
    avatar_archive = SocialAvatarArchive(settings)

    existing_avatar = avatar_archive.avatar_dir / f"{sha256(avatar_url.encode()).hexdigest()}.webp"
    existing_avatar.write_bytes(b"RIFF-VALID-AVATAR")

    with Session(engine) as session:
        author = Author(name="Author")
        session.add(author)
        session.flush()
        account = SocialAccount(author_id=author.id, handle="creator", status="confirmed")
        session.add(account)
        session.flush()
        post = SocialPost(
            account_id=account.id,
            platform_post_id="3",
            url="https://x.com/creator/status/3",
            content_hash="h3",
            posted_at=datetime.now(UTC),
        )
        session.add(post)
        session.flush()

        async with httpx.AsyncClient(transport=httpx.MockTransport(slow_handler)) as client:
            archived_avatar = await SocialAvatarArchive(settings, client).archive(avatar_url)
            assert archived_avatar == existing_avatar
            assert existing_avatar.read_bytes() == b"RIFF-VALID-AVATAR"

            archive = SocialMediaArchive(session, settings, client)
            task = asyncio.create_task(
                archive.archive(post.id, 0, "https://pbs.twimg.com/media/slow.jpg")
            )
            await asyncio.sleep(0.1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    assert list((tmp_path / "archive").glob(".tmp-*")) == []
    assert list((tmp_path / "archive").glob("*.webp")) == []


@pytest.mark.asyncio
async def test_ocr_subprocess_killed_and_reaped_on_cancel_and_timeout(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawned_pids: list[int] = []
    real_exec = asyncio.create_subprocess_exec

    async def fake_tesseract_exec(*args, **kwargs):
        # Replace `tesseract <path> stdout -l jpn+eng` with a python child process that sleeps.
        proc = await real_exec(
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
            stdout=kwargs.get("stdout"),
            stderr=kwargs.get("stderr"),
        )
        spawned_pids.append(proc.pid)
        return proc

    monkeypatch.setattr(
        "app.modules.social.ocr.asyncio.create_subprocess_exec", fake_tesseract_exec
    )
    monkeypatch.setattr("app.modules.social.ocr.shutil.which", lambda _: "/usr/bin/tesseract")

    settings = Settings(
        social_media_dir=tmp_path,
        social_ocr_enabled=True,
        social_ocr_timeout_seconds=0.15,
        _env_file=None,
    )
    ocr = LocalMediaOcr(settings)
    img_path = tmp_path / "sample.png"
    img_path.write_bytes(_make_image_bytes("RGB", (100, 100), (0, 0, 0), "PNG"))

    # 1. Timeout kills and reaps subprocess cleanly.
    out_timeout = await ocr._recognize(str(img_path))
    assert out_timeout is None
    assert len(spawned_pids) == 1
    pid1 = spawned_pids[0]
    with pytest.raises(ProcessLookupError):
        os.kill(pid1, 0)

    # 2. Caller cancellation kills and reaps subprocess and re-raises CancelledError.
    settings.social_ocr_timeout_seconds = 30.0
    task = asyncio.create_task(ocr._recognize(str(img_path)))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(spawned_pids) == 2
    pid2 = spawned_pids[1]
    with pytest.raises(ProcessLookupError):
        os.kill(pid2, 0)


@pytest.mark.asyncio
async def test_ocr_preserves_order_bounds_and_exif_rotation_for_tesseract(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.modules.social.ocr.shutil.which", lambda _: "/usr/bin/tesseract")

    rotated = _make_image_bytes("RGB", (400, 200), (255, 255, 255), "JPEG", exif_orientation=6)
    small_ok = _make_image_bytes("RGB", (300, 300), (255, 255, 255), "PNG")

    def handler(request: httpx.Request) -> httpx.Response:
        if "rot" in request.url.path:
            return httpx.Response(200, content=rotated)
        return httpx.Response(200, content=small_ok)

    observed_args: list[tuple[str, ...]] = []
    observed_sizes: list[tuple[int, int]] = []
    real_ocr = LocalMediaOcr

    class FakeProc:
        returncode = 0

        def __init__(self, payload: bytes) -> None:
            self._payload = payload

        async def communicate(self):
            return self._payload, b""

    idx_counter = 0

    async def fake_exec(*args, **kwargs):
        nonlocal idx_counter
        observed_args.append(tuple(str(a) for a in args))
        with Image.open(args[1]) as im:
            observed_sizes.append(im.size)
        idx_counter += 1
        if idx_counter == 1:
            # Exceeds 6000 chars -> verify per-image 6000 char bound is kept
            return FakeProc(("日本語テキスト" * 1500).encode())
        return FakeProc(f"page-{idx_counter}".encode())

    monkeypatch.setattr("app.modules.social.ocr.asyncio.create_subprocess_exec", fake_exec)

    settings = Settings(social_media_dir=tmp_path, social_ocr_enabled=True, _env_file=None)
    media = [
        {"type": "image", "url": "https://pbs.twimg.com/media/rot.jpg"},
        {"type": "video", "url": "https://video.twimg.com/media/skip.mp4"},
        {"type": "image", "url": "https://pbs.twimg.com/media/p2.png"},
        {"type": "image", "url": "https://pbs.twimg.com/media/p3.png"},
        {"type": "image", "url": "https://pbs.twimg.com/media/p4_ignored.png"},
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        res = await real_ocr(settings, client=client).extract_result(media)

    assert res.status == "text"
    # Only first 4 media items considered, video skipped -> 3 images processed in order.
    assert len(observed_args) == 3
    assert observed_args[0][0] == "tesseract"
    assert observed_args[0][3:] == ("-l", "jpn+eng")
    # EXIF 6 (400x200) must be transposed to upright (200x400) before tesseract!
    assert observed_sizes == [(200, 400), (300, 300), (300, 300)]
    parts = (res.text or "").split("\n\n")
    assert len(parts) == 3
    assert len(parts[0]) == 6000
    assert parts[1] == "page-2"
    assert parts[2] == "page-3"


@pytest.mark.asyncio
async def test_ocr_partial_retry_and_content_hash_protection(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'ocr-retry.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    settings = Settings(
        social_agent_enabled=False,
        social_ocr_enabled=True,
        social_media_dir=tmp_path,
        social_media_cache_max_bytes=0,
        _env_file=None,
    )

    with factory() as session:
        author = Author(name="artist")
        session.add(author)
        session.flush()
        account = SocialAccount(author_id=author.id, handle="creator", status="confirmed")
        session.add(account)
        session.flush()
        service = SocialSyncService(session, settings)
        post_item = CollectorPost(
            id="100",
            text="新刊サンプル",
            url="https://x.com/creator/status/100",
            posted_at=datetime.now(UTC),
            media=[
                {"type": "image", "url": "https://pbs.twimg.com/media/1.jpg"},
                {"type": "image", "url": "https://pbs.twimg.com/media/2.jpg"},
            ],
        )
        service.queue_posts(account, [post_item])
        session.commit()
        post_id = session.query(SocialPost.id).scalar()
        account_id = account.id

    # Step 1: img0 succeeds, img1 fails -> ocr_status="retry", text="第1页"
    results = [
        OcrResult("retry", text="第1页", error="TimeoutError"),
        OcrResult("retry", text="第2页", error="TimeoutError"),
        OcrResult("text", text="第1页\n\n第2页"),
    ]

    async def fake_extract(self, media, local_paths=None):
        return results.pop(0)

    monkeypatch.setattr("app.modules.social.ocr.LocalMediaOcr.extract_result", fake_extract)

    with factory() as session:
        await SocialSyncService(session, settings).process_post(post_id)
        p = session.get(SocialPost, post_id)
        st = session.get(SocialPostProcessing, post_id)
        assert st.ocr_status == "retry"
        assert p.ocr_text == "第1页"
        # Simulate scheduler re-queueing OCR retry
        st.status = "pending"
        st.backfill_only = True
        session.commit()

    # Step 2: retry where img0 fails and img1 succeeds -> stays "retry", keeps "第1页"
    with factory() as session:
        await SocialSyncService(session, settings).process_post(post_id)
        p = session.get(SocialPost, post_id)
        st = session.get(SocialPostProcessing, post_id)
        assert st.ocr_status == "retry"
        assert p.ocr_text == "第1页\n\n第2页"
        st.status = "pending"
        session.commit()

    # Step 3: full success -> ocr_status="text"
    with factory() as session:
        await SocialSyncService(session, settings).process_post(post_id)
        st = session.get(SocialPostProcessing, post_id)
        assert st.ocr_status == "text"
        assert session.get(SocialPost, post_id).ocr_text == "第1页\n\n第2页"

    # Step 4: content_hash changes mid-OCR -> stale OCR result must NOT overwrite new post
    with factory() as session:
        st = session.get(SocialPostProcessing, post_id)
        st.status = "pending"
        st.ocr_status = "pending"
        st.backfill_only = False
        session.commit()

    async def mutating_ocr(self, media, local_paths=None):
        with factory() as other:
            other_acc = other.get(SocialAccount, account_id)
            SocialSyncService(other, settings).queue_posts(
                other_acc,
                [
                    CollectorPost(
                        id="100",
                        text="新内容已更新",
                        url="https://x.com/creator/status/100",
                        posted_at=datetime.now(UTC),
                        media=[{"type": "image", "url": "https://pbs.twimg.com/media/new.jpg"}],
                    )
                ],
            )
            other.commit()
        return OcrResult("text", text="旧帖子残留文字")

    monkeypatch.setattr("app.modules.social.ocr.LocalMediaOcr.extract_result", mutating_ocr)
    with factory() as session:
        with pytest.raises(RuntimeError, match="发生变化"):
            await SocialSyncService(session, settings).process_post(post_id)
        session.rollback()
        refreshed = session.get(SocialPost, post_id)
        assert refreshed.text == "新内容已更新"
        assert refreshed.ocr_text is None
        assert session.get(SocialPostProcessing, post_id).ocr_status == "pending"


@pytest.mark.asyncio
async def test_decode_and_ocr_concurrency_gates_stay_bounded() -> None:
    gate = AsyncConcurrencyGate()
    active = 0
    peak = 0

    async def worker(idx: int) -> int:
        nonlocal active, peak
        async with gate.slot(2):
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.03)
            active -= 1
        return idx

    tasks = [asyncio.create_task(worker(i)) for i in range(8)]
    await asyncio.sleep(0.02)
    tasks[3].cancel()
    results = await asyncio.gather(*tasks, return_exceptions=True)

    assert peak == 2
    assert gate.in_flight == 0
    assert isinstance(results[3], asyncio.CancelledError)
    assert [r for r in results if not isinstance(r, BaseException)] == [0, 1, 2, 4, 5, 6, 7]

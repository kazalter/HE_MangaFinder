from datetime import UTC, datetime
from io import BytesIO

import httpx
import pytest
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.base import Base
from app.db.models import Author, SocialAccount, SocialMediaAsset, SocialPost
from app.modules.social import media_archive
from app.modules.social.media_archive import SocialAvatarArchive, SocialMediaArchive


@pytest.mark.asyncio
async def test_avatar_archive_is_local_and_reused(tmp_path) -> None:
    source = BytesIO()
    Image.new("RGB", (1200, 1200), "#445566").save(source, format="PNG")
    requests = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, content=source.getvalue())

    settings = Settings(social_media_dir=tmp_path, _env_file=None)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        archive = SocialAvatarArchive(settings, client)
        first = await archive.archive(
            "https://pbs.twimg.com/profile_images/42/avatar.jpg"
        )
        second = await archive.archive(
            "https://pbs.twimg.com/profile_images/42/avatar.jpg"
        )

    assert first == second
    assert first is not None
    assert first.parent == tmp_path / "avatars"
    assert first.read_bytes().startswith(b"RIFF")
    assert requests == 1


def test_social_media_http_client_uses_x_proxy(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_client(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(media_archive.httpx, "AsyncClient", fake_client)
    settings = Settings(x_proxy_url="http://proxy:7890", _env_file=None)

    media_archive._http_client(settings)

    assert captured["proxy"] == "http://proxy:7890"
    assert captured["follow_redirects"] is True


@pytest.mark.asyncio
async def test_archive_compresses_and_quota_evicts_only_local_copy(tmp_path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        author = Author(name="作家")
        session.add(author)
        session.flush()
        account = SocialAccount(author_id=author.id, handle="creator", status="confirmed")
        session.add(account)
        session.flush()
        post = SocialPost(
            account_id=account.id,
            platform_post_id="1",
            text="图片动态",
            url="https://x.com/creator/status/1",
            media=[{"type": "image", "url": "https://pbs.twimg.com/media/test.jpg"}],
            links=[],
            raw_metadata={},
            content_hash="1",
            posted_at=datetime.now(UTC),
        )
        session.add(post)
        session.flush()

        source = BytesIO()
        Image.new("RGB", (2400, 1600), "#b85c45").save(source, format="PNG")

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=source.getvalue())

        settings = Settings(
            social_media_dir=tmp_path,
            social_media_max_dimension=800,
            social_media_cache_max_bytes=1024**3,
            _env_file=None,
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            archive = SocialMediaArchive(session, settings, client)
            paths = await archive.archive_post(post)

        asset = session.query(SocialMediaAsset).one()
        assert paths[0].is_file()
        assert asset.status == "archived"
        assert max(asset.width or 0, asset.height or 0) == 800
        assert asset.byte_size < len(source.getvalue())

        settings.social_media_cache_max_bytes = 1
        assert archive.enforce_quota() == 1
        assert asset.status == "evicted"
        assert asset.byte_size == 0
        assert not paths[0].exists()
        assert session.get(SocialPost, post.id) is not None

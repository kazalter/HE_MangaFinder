import asyncio
import logging
import os
import tempfile
import threading
from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from typing import BinaryIO
from urllib.parse import urlparse

import httpx
from PIL import Image, ImageOps
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.observability import record_metric
from app.db.models import (
    ActivityItem,
    ActivityItemPost,
    ReleaseSignal,
    ReleaseSignalPost,
    SocialMediaAsset,
    SocialPost,
)
from app.modules.social.media_io import (
    DECODE_GATE,
    download_bounded_to_file,
    validate_image_dimensions,
)

logger = logging.getLogger(__name__)
ALLOWED_MEDIA_HOSTS = {"pbs.twimg.com"}
MAX_SOURCE_BYTES = 32 * 1024 * 1024
MAX_AVATAR_BYTES = 8 * 1024 * 1024
_DEFAULT_MAX_PIXELS = 40_000_000
_SPOOL_MEMORY_BYTES = 1024 * 1024


def _http_client(settings: Settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=30,
        follow_redirects=True,
        proxy=settings.x_proxy_url or None,
    )


def _encode_image_to_handle(
    source: BinaryIO,
    output: BinaryIO,
    maximum_dimension: int,
    quality: int,
    *,
    max_pixels: int = _DEFAULT_MAX_PIXELS,
) -> tuple[int, int]:
    """Decode, validate pixel budget, orient, resize, and save as WebP into `output`."""
    record_metric("media_decode_count")
    source.seek(0)
    with Image.open(source) as opened:
        validate_image_dimensions(opened, max_pixels)
        maximum = max(320, maximum_dimension)
        working: Image.Image = opened
        try:
            # Convert palette images before resampling so alpha and colors survive LANCZOS.
            if working.mode in {"P", "PA"}:
                target_mode = (
                    "RGBA" if ("transparency" in working.info or working.mode == "PA") else "RGB"
                )
                working = working.convert(target_mode)
            working.thumbnail((maximum, maximum), Image.Resampling.LANCZOS)
            # Ensure truncated small images (where thumbnail is a no-op) fail before saving.
            working.load()
            transposed = ImageOps.exif_transpose(working)
            if transposed is not working:
                if working is not opened:
                    working.close()
                working = transposed
            if working.mode not in {"RGB", "RGBA"}:
                target_mode = (
                    "RGBA"
                    if ("transparency" in working.info or "A" in working.mode)
                    else "RGB"
                )
                converted = working.convert(target_mode)
                if working is not opened:
                    working.close()
                working = converted
            width, height = working.size
            working.save(
                output,
                format="WEBP",
                quality=min(95, max(40, quality)),
                method=6,
            )
            return width, height
        finally:
            if working is not opened:
                working.close()


def _encode_webp(
    raw: bytes | BinaryIO,
    maximum_dimension: int,
    quality: int,
    *,
    max_pixels: int = _DEFAULT_MAX_PIXELS,
) -> tuple[bytes, int, int]:
    source = BytesIO(raw) if isinstance(raw, (bytes, bytearray)) else raw
    output = BytesIO()
    width, height = _encode_image_to_handle(
        source,
        output,
        maximum_dimension,
        quality,
        max_pixels=max_pixels,
    )
    return output.getvalue(), width, height


def _encode_webp_to_temp(
    source: BinaryIO,
    temp_dir: Path,
    maximum_dimension: int,
    quality: int,
    *,
    max_pixels: int = _DEFAULT_MAX_PIXELS,
    cancelled: threading.Event | None = None,
) -> tuple[Path, str, int, int, int]:
    """Encode `source` to a temporary WebP file in `temp_dir` and return its hash and metrics."""
    if cancelled is not None and cancelled.is_set():
        raise asyncio.CancelledError
    temp_dir.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=temp_dir, prefix=".tmp-", suffix=".webp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w+b") as handle:
            width, height = _encode_image_to_handle(
                source,
                handle,
                maximum_dimension,
                quality,
                max_pixels=max_pixels,
            )
            if cancelled is not None and cancelled.is_set():
                raise asyncio.CancelledError
            handle.flush()
            handle.seek(0)
            hasher = sha256()
            byte_size = 0
            while chunk := handle.read(65536):
                hasher.update(chunk)
                byte_size += len(chunk)
        return temporary, hasher.hexdigest(), byte_size, width, height
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _write_atomically(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


async def _run_bounded_encode(
    spool: BinaryIO,
    temp_dir: Path,
    maximum_dimension: int,
    quality: int,
    *,
    max_pixels: int,
    concurrency: int,
) -> tuple[Path, str, int, int, int]:
    """Run WebP encoding under the shared decode concurrency gate with safe cancellation cleanup."""
    async with DECODE_GATE.slot(concurrency):
        cancelled = threading.Event()
        task = asyncio.create_task(
            asyncio.to_thread(
                _encode_webp_to_temp,
                spool,
                temp_dir,
                maximum_dimension,
                quality,
                max_pixels=max_pixels,
                cancelled=cancelled,
            )
        )
        try:
            return await task
        except BaseException:
            cancelled.set()
            try:
                temp_path, *_ = await task
            except BaseException:
                pass
            else:
                temp_path.unlink(missing_ok=True)
            raise


class SocialAvatarArchive:
    """Small durable avatar cache stored alongside the HDD-backed social archive."""

    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.client = client
        self.avatar_dir = settings.social_media_dir / "avatars"
        self.avatar_dir.mkdir(parents=True, exist_ok=True)

    def local_file(self, source_url: str) -> Path | None:
        path = self.avatar_dir / f"{sha256(source_url.encode()).hexdigest()}.webp"
        return path if path.is_file() else None

    async def archive(self, source_url: str) -> Path | None:
        parsed = urlparse(source_url)
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_MEDIA_HOSTS:
            return None
        existing = self.local_file(source_url)
        if existing:
            return existing

        owns_client = self.client is None
        client = self.client or _http_client(self.settings)
        temp_webp: Path | None = None
        try:
            with tempfile.SpooledTemporaryFile(
                max_size=_SPOOL_MEMORY_BYTES, mode="w+b", dir=self.avatar_dir
            ) as spool:
                await download_bounded_to_file(
                    client,
                    source_url,
                    spool,
                    headers={"User-Agent": self.settings.user_agent},
                    max_bytes=MAX_AVATAR_BYTES,
                )
                temp_webp, _, _, _, _ = await _run_bounded_encode(
                    spool,
                    self.avatar_dir,
                    min(800, self.settings.social_media_max_dimension),
                    self.settings.social_media_webp_quality,
                    max_pixels=self.settings.social_media_max_pixels,
                    concurrency=self.settings.social_media_decode_concurrency,
                )
            path = self.avatar_dir / f"{sha256(source_url.encode()).hexdigest()}.webp"
            if not path.exists():
                os.replace(temp_webp, path)
                temp_webp = None
            return path
        except Exception as exc:
            logger.info("Social avatar archive skipped %s: %s", source_url, exc)
            return None
        finally:
            if temp_webp is not None:
                temp_webp.unlink(missing_ok=True)
            if owns_client:
                await client.aclose()


class SocialMediaArchive:
    def __init__(
        self,
        session: Session,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.session = session
        self.settings = settings
        self.client = client
        self.archive_dir = settings.social_media_dir / "archive"
        self.archive_dir.mkdir(parents=True, exist_ok=True)

    def asset(self, post_id: int, media_index: int) -> SocialMediaAsset | None:
        return self.session.scalar(
            select(SocialMediaAsset).where(
                SocialMediaAsset.post_id == post_id,
                SocialMediaAsset.media_index == media_index,
            )
        )

    def local_file(self, asset: SocialMediaAsset) -> Path | None:
        if not asset.local_path:
            return None
        path = (self.settings.social_media_dir / asset.local_path).resolve()
        root = self.settings.social_media_dir.resolve()
        if root not in path.parents or not path.is_file():
            return None
        return path

    async def archive_post(self, post: SocialPost) -> dict[int, Path]:
        if self.settings.social_media_cache_max_bytes <= 0:
            return {}
        paths: dict[int, Path] = {}
        for index, item in enumerate(post.media):
            if item.get("type") != "image" or not item.get("url"):
                continue
            asset = await self.archive(post.id, index, str(item["url"]))
            path = self.local_file(asset)
            if path:
                paths[index] = path
        self.enforce_quota()
        return paths

    async def archive(
        self, post_id: int, media_index: int, source_url: str
    ) -> SocialMediaAsset:
        # Avoid flushing earlier dirty ORM changes before remote HTTP I/O.
        with self.session.no_autoflush:
            asset = self.asset(post_id, media_index)
        previous_path: Path | None = None
        existing_source_url: str | None = None
        is_new = asset is None
        if asset is not None:
            local = self.local_file(asset)
            if local and asset.status == "archived" and asset.source_url == source_url:
                asset.last_accessed_at = datetime.now(UTC)
                return asset
            previous_path = local
            existing_source_url = asset.source_url
        source_changed = not is_new and existing_source_url != source_url

        parsed = urlparse(source_url)
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_MEDIA_HOSTS:
            if asset is None:
                asset = SocialMediaAsset(
                    post_id=post_id,
                    media_index=media_index,
                    source_url=source_url,
                    status="failed",
                    error="不允许归档该媒体来源",
                )
                self.session.add(asset)
            else:
                asset.source_url = source_url
                asset.status = "failed"
                asset.error = "不允许归档该媒体来源"
            self.session.flush()
            return asset

        owns_client = self.client is None
        client = self.client or _http_client(self.settings)
        archived_file: tuple[str, str, int, int, int, Path] | None = None
        error_message: str | None = None
        temp_webp: Path | None = None
        try:
            with tempfile.SpooledTemporaryFile(
                max_size=_SPOOL_MEMORY_BYTES, mode="w+b", dir=self.archive_dir
            ) as spool:
                await download_bounded_to_file(
                    client,
                    source_url,
                    spool,
                    headers={"User-Agent": self.settings.user_agent},
                    max_bytes=MAX_SOURCE_BYTES,
                )
                temp_webp, digest, byte_size, width, height = await _run_bounded_encode(
                    spool,
                    self.archive_dir,
                    self.settings.social_media_max_dimension,
                    self.settings.social_media_webp_quality,
                    max_pixels=self.settings.social_media_max_pixels,
                    concurrency=self.settings.social_media_decode_concurrency,
                )
            filename = f"archive/{post_id}-{media_index}-{digest[:20]}.webp"
            path = self.settings.social_media_dir / filename
            if not path.exists():
                os.replace(temp_webp, path)
                temp_webp = None
            archived_file = (filename, digest, byte_size, width, height, path)
        except Exception as exc:
            error_message = str(exc)[:1000]
            logger.info("Social media archive skipped %s: %s", source_url, exc)
        finally:
            if temp_webp is not None:
                temp_webp.unlink(missing_ok=True)
            if owns_client:
                await client.aclose()

        if asset is None:
            asset = SocialMediaAsset(
                post_id=post_id,
                media_index=media_index,
                source_url=source_url,
            )
            self.session.add(asset)
        else:
            asset.source_url = source_url

        if archived_file is not None:
            filename, digest, byte_size, width, height, path = archived_file
            asset.local_path = filename
            asset.content_hash = digest
            asset.mime_type = "image/webp"
            asset.byte_size = byte_size
            asset.width = width
            asset.height = height
            asset.status = "archived"
            asset.last_accessed_at = datetime.now(UTC)
            asset.error = None
            if previous_path and previous_path != path:
                previous_path.unlink(missing_ok=True)
        else:
            asset.status = "failed"
            asset.error = error_message
            if source_changed and previous_path:
                previous_path.unlink(missing_ok=True)
                asset.local_path = None
                asset.byte_size = 0

        self.session.flush()
        return asset

    def _encode(self, raw: bytes) -> tuple[bytes, int, int]:
        return _encode_webp(
            raw,
            self.settings.social_media_max_dimension,
            self.settings.social_media_webp_quality,
            max_pixels=self.settings.social_media_max_pixels,
        )

    def pin_post(self, post_id: int, importance: str = "high") -> None:
        assets = self.session.scalars(
            select(SocialMediaAsset).where(SocialMediaAsset.post_id == post_id)
        )
        for asset in assets:
            asset.pinned = True
            asset.importance = importance
        self.session.flush()

    def apply_existing_importance(self, asset: SocialMediaAsset) -> None:
        signal = self.session.scalar(
            select(ReleaseSignal.id)
            .join(ReleaseSignalPost, ReleaseSignalPost.signal_id == ReleaseSignal.id)
            .where(
                ReleaseSignalPost.post_id == asset.post_id,
                ReleaseSignal.status.in_(["pending", "confirmed", "linked"]),
            )
            .limit(1)
        )
        if signal is not None:
            asset.pinned = True
            asset.importance = "critical"
            self.session.flush()
            return
        importance = self.session.scalar(
            select(ActivityItem.importance)
            .join(ActivityItemPost, ActivityItemPost.activity_id == ActivityItem.id)
            .where(ActivityItemPost.post_id == asset.post_id)
            .limit(1)
        )
        if importance in {"high", "critical"}:
            asset.pinned = True
            asset.importance = importance
            self.session.flush()

    def enforce_quota(self) -> int:
        maximum = max(0, self.settings.social_media_cache_max_bytes)
        total = int(
            self.session.scalar(
                select(func.coalesce(func.sum(SocialMediaAsset.byte_size), 0)).where(
                    SocialMediaAsset.status == "archived"
                )
            )
            or 0
        )
        if maximum <= 0 or total <= maximum:
            return 0
        target = int(maximum * min(0.95, max(0.5, self.settings.social_media_cache_target_ratio)))
        rank = case(
            (SocialMediaAsset.importance == "low", 0),
            (SocialMediaAsset.importance == "normal", 1),
            (SocialMediaAsset.importance == "high", 2),
            else_=3,
        )
        candidates = list(
            self.session.scalars(
                select(SocialMediaAsset)
                .where(
                    SocialMediaAsset.status == "archived",
                    SocialMediaAsset.pinned.is_(False),
                )
                .order_by(rank, SocialMediaAsset.last_accessed_at, SocialMediaAsset.id)
                .limit(500)
            )
        )
        removed = 0
        for asset in candidates:
            if total <= target:
                break
            size = asset.byte_size
            path = self.local_file(asset)
            if path:
                shared = self.session.scalar(
                    select(SocialMediaAsset.id).where(
                        SocialMediaAsset.id != asset.id,
                        SocialMediaAsset.local_path == asset.local_path,
                        SocialMediaAsset.status == "archived",
                    ).limit(1)
                )
                if shared is None:
                    path.unlink(missing_ok=True)
            asset.local_path = None
            asset.byte_size = 0
            asset.status = "evicted"
            total -= size
            removed += 1
        self.session.flush()
        return removed

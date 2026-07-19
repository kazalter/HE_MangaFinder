import logging
from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

import httpx
from PIL import Image, ImageOps
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import (
    ActivityItem,
    ActivityItemPost,
    ReleaseSignal,
    ReleaseSignalPost,
    SocialMediaAsset,
    SocialPost,
)

logger = logging.getLogger(__name__)
ALLOWED_MEDIA_HOSTS = {"pbs.twimg.com"}
MAX_SOURCE_BYTES = 32 * 1024 * 1024


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
        asset = self.asset(post_id, media_index)
        previous_path: Path | None = None
        source_changed = False
        is_new = asset is None
        if asset is None:
            asset = SocialMediaAsset(
                post_id=post_id,
                media_index=media_index,
                source_url=source_url,
                status="pending",
            )
        else:
            local = self.local_file(asset)
            if local and asset.status == "archived" and asset.source_url == source_url:
                asset.last_accessed_at = datetime.now(UTC)
                return asset
            previous_path = local
            source_changed = asset.source_url != source_url
            asset.source_url = source_url
            asset.status = "pending"
            asset.error = None

        parsed = urlparse(source_url)
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_MEDIA_HOSTS:
            asset.status = "failed"
            asset.error = "不允许归档该媒体来源"
            if is_new:
                self.session.add(asset)
            self.session.flush()
            return asset

        owns_client = self.client is None
        client = self.client or httpx.AsyncClient(timeout=30, follow_redirects=True)
        try:
            response = await client.get(
                source_url, headers={"User-Agent": self.settings.user_agent}
            )
            response.raise_for_status()
            raw = response.content
            if len(raw) > MAX_SOURCE_BYTES:
                raise ValueError("图片超过 32 MB 归档上限")
            encoded, width, height = self._encode(raw)
            digest = sha256(encoded).hexdigest()
            filename = f"archive/{post_id}-{media_index}-{digest[:20]}.webp"
            path = self.settings.social_media_dir / filename
            if not path.exists():
                path.write_bytes(encoded)
            asset.local_path = filename
            asset.content_hash = digest
            asset.mime_type = "image/webp"
            asset.byte_size = len(encoded)
            asset.width = width
            asset.height = height
            asset.status = "archived"
            asset.last_accessed_at = datetime.now(UTC)
            asset.error = None
            if previous_path and previous_path != path:
                previous_path.unlink(missing_ok=True)
        except Exception as exc:
            asset.status = "failed"
            asset.error = str(exc)[:1000]
            if source_changed and previous_path:
                previous_path.unlink(missing_ok=True)
                asset.local_path = None
                asset.byte_size = 0
            logger.info("Social media archive skipped %s: %s", source_url, exc)
        finally:
            if owns_client:
                await client.aclose()
        if is_new:
            self.session.add(asset)
        self.session.flush()
        return asset

    def _encode(self, raw: bytes) -> tuple[bytes, int, int]:
        with Image.open(BytesIO(raw)) as opened:
            opened.load()
            image = ImageOps.exif_transpose(opened)
            if image.mode not in {"RGB", "RGBA"}:
                image = image.convert("RGBA" if "transparency" in image.info else "RGB")
            maximum = max(320, self.settings.social_media_max_dimension)
            image.thumbnail((maximum, maximum), Image.Resampling.LANCZOS)
            width, height = image.size
            output = BytesIO()
            image.save(
                output,
                format="WEBP",
                quality=min(95, max(40, self.settings.social_media_webp_quality)),
                method=6,
            )
            return output.getvalue(), width, height

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

import asyncio
import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from app.providers.errors import ProviderError
from app.providers.registry import ProviderRegistry

_EXTENSIONS = {
    "image/avif": ".avif",
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


@dataclass(frozen=True, slots=True)
class CachedCover:
    path: Path
    content_type: str


class CoverCacheService:
    def __init__(self, cache_dir: Path, providers: ProviderRegistry) -> None:
        self.cache_dir = cache_dir.resolve()
        self.providers = providers
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, asyncio.Lock] = {}
        self._fetch_limit = asyncio.Semaphore(6)

    async def get(
        self, provider_name: str, external_id: str, cover_url: str
    ) -> CachedCover:
        provider = self.providers.get_optional(provider_name)
        if provider is None:
            raise ProviderError("封面来源已停用")
        safe_id = re.sub(r"[^a-zA-Z0-9._-]+", "_", external_id).strip("._")[:100]
        if not safe_id:
            raise ProviderError("封面来源 ID 无效")
        digest = sha256(
            f"{provider_name}\0{external_id}\0{cover_url}".encode()
        ).hexdigest()
        folder = self.cache_dir / provider_name / safe_id
        cached = self._existing(folder, digest)
        if cached:
            return cached

        work_key = f"{provider_name}:{external_id}"
        lock = self._locks.setdefault(work_key, asyncio.Lock())
        async with lock:
            cached = self._existing(folder, digest)
            if cached:
                return cached
            async with self._fetch_limit:
                image = None
                for attempt in range(2):
                    try:
                        image = await provider.fetch_cover(external_id, cover_url)
                        break
                    except ProviderError:
                        if attempt == 1:
                            raise
                        await asyncio.sleep(0.25)
                if image is None:
                    raise ProviderError("封面下载失败")
            content_type = image.content_type.split(";", 1)[0].lower()
            extension = _EXTENSIONS.get(content_type)
            if extension is None:
                raise ProviderError(f"不支持的封面格式: {content_type or 'unknown'}")
            folder.mkdir(parents=True, exist_ok=True)
            destination = folder / f"{digest}{extension}"
            temporary = folder / f".{digest}.part"
            try:
                temporary.write_bytes(image.content)
                temporary.replace(destination)
            except Exception:
                temporary.unlink(missing_ok=True)
                raise
            for old_path in folder.iterdir():
                if old_path != destination and old_path.is_file():
                    old_path.unlink(missing_ok=True)
            return CachedCover(destination, content_type)

    @staticmethod
    def _existing(folder: Path, digest: str) -> CachedCover | None:
        if not folder.is_dir():
            return None
        for content_type, extension in _EXTENSIONS.items():
            path = folder / f"{digest}{extension}"
            if path.is_file():
                return CachedCover(path, content_type)
        return None

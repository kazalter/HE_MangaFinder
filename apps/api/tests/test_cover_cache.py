from pathlib import Path

from app.db.models import Work, WorkSource
from app.modules.media.service import CoverCacheService
from app.modules.media.urls import work_cover_url
from app.providers.base import RemoteImage
from app.providers.errors import ProviderError
from app.providers.registry import ProviderRegistry


class FakeCoverProvider:
    name = "nhentai"
    display_name = "nHentai"
    capabilities = frozenset()

    def __init__(self) -> None:
        self.calls = 0
        self.failures_remaining = 0

    async def fetch_cover(self, work_external_id: str, cover_url: str) -> RemoteImage:
        self.calls += 1
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise ProviderError("temporary failure")
        return RemoteImage(
            content=f"{work_external_id}:{cover_url}".encode(),
            content_type="image/webp",
        )


async def test_caches_cover_atomically_and_reuses_it(tmp_path: Path) -> None:
    provider = FakeCoverProvider()
    service = CoverCacheService(tmp_path, ProviderRegistry([provider]))

    first = await service.get("nhentai", "123", "https://t.nhentai.net/one.webp")
    second = await service.get("nhentai", "123", "https://t.nhentai.net/one.webp")

    assert first == second
    assert first.path.read_bytes() == b"123:https://t.nhentai.net/one.webp"
    assert first.content_type == "image/webp"
    assert provider.calls == 1
    assert not list(tmp_path.rglob("*.part"))


async def test_cover_url_change_replaces_old_cached_file(tmp_path: Path) -> None:
    provider = FakeCoverProvider()
    service = CoverCacheService(tmp_path, ProviderRegistry([provider]))

    first = await service.get("nhentai", "123", "https://t.nhentai.net/one.webp")
    second = await service.get("nhentai", "123", "https://t.nhentai.net/two.webp")

    assert not first.path.exists()
    assert second.path.exists()
    assert provider.calls == 2


async def test_retries_one_transient_cover_failure(tmp_path: Path) -> None:
    provider = FakeCoverProvider()
    provider.failures_remaining = 1
    service = CoverCacheService(tmp_path, ProviderRegistry([provider]))

    cached = await service.get("nhentai", "123", "https://t.nhentai.net/one.webp")

    assert cached.path.exists()
    assert provider.calls == 2


def test_serializes_supported_cover_as_versioned_same_origin_url() -> None:
    work = Work(id=42, title="Example", cover_url="https://t.nhentai.net/cover.webp")
    work.sources.append(
        WorkSource(
            provider="nhentai",
            external_id="123",
            source_url="https://nhentai.net/g/123/",
        )
    )

    value = work_cover_url(work)

    assert value is not None
    assert value.startswith("/api/works/42/cover?v=")

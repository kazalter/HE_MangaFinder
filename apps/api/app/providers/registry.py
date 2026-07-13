from collections.abc import Iterable

from app.core.config import Settings
from app.providers.base import SourceProvider
from app.providers.hanimeone import HanimeOneProvider
from app.providers.mangadex import MangaDexProvider
from app.providers.wnacg import WnacgProvider


class ProviderRegistry:
    def __init__(self, providers: Iterable[SourceProvider]) -> None:
        self._providers = {provider.name: provider for provider in providers}

    def all(self) -> list[SourceProvider]:
        return list(self._providers.values())

    def get(self, name: str) -> SourceProvider:
        return self._providers[name]

    async def close(self) -> None:
        for provider in self._providers.values():
            await provider.close()


def build_registry(settings: Settings) -> ProviderRegistry:
    providers: list[SourceProvider] = [
            MangaDexProvider(
                user_agent=settings.user_agent,
                use_data_saver=settings.use_data_saver,
                chapter_languages=settings.chapter_language_list,
            ),
            WnacgProvider(
                user_agent=settings.user_agent,
                base_urls=settings.wnacg_base_url_list,
                cookie=settings.wnacg_cookie,
                max_search_pages=settings.wnacg_max_search_pages,
            ),
        ]
    if settings.hanimeone_enabled:
        providers.append(
            HanimeOneProvider(
                user_agent=settings.user_agent,
                base_url=settings.hanimeone_base_url,
                proxy_url=settings.hanimeone_proxy_url,
                cookie=settings.hanimeone_cookie,
            )
        )
    return ProviderRegistry(providers)

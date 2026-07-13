import logging
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.modules.authors.repository import AuthorRepository
from app.modules.catalog.aggregation import AggregationService, CoverHasher
from app.modules.catalog.repository import CatalogRepository
from app.providers.base import sort_discovered_works
from app.providers.errors import AuthorNotFoundError
from app.providers.registry import ProviderRegistry

logger = logging.getLogger(__name__)


class DiscoveryService:
    def __init__(self, session: Session, providers: ProviderRegistry) -> None:
        self.session = session
        self.providers = providers

    async def discover_author(self, author_id: int) -> int:
        author = AuthorRepository(self.session).get(author_id)
        if author is None:
            return 0

        catalog = CatalogRepository(self.session)
        cover_hasher = CoverHasher()
        aggregation = AggregationService(self.session, cover_hasher)
        discovered = 0
        errors: list[str] = []
        successful_providers = 0
        try:
            for provider in self.providers.all():
                try:
                    works = sort_discovered_works(
                        await provider.discover_by_author(author.name)
                    )
                    for work in works:
                        stored_work = catalog.upsert(author.id, provider.name, work)
                        await aggregation.assign(stored_work, author)
                    discovered += len(works)
                    successful_providers += 1
                    self.session.commit()
                except AuthorNotFoundError:
                    successful_providers += 1
                    self.session.rollback()
                except Exception as exc:
                    self.session.rollback()
                    errors.append(f"{provider.display_name}: {exc}")
        finally:
            await cover_hasher.close()

        if errors and successful_providers == 0:
            raise RuntimeError("; ".join(errors))
        if errors:
            logger.warning("部分漫画来源查询失败: %s", "; ".join(errors))

        author = AuthorRepository(self.session).get(author_id)
        if author:
            author.last_checked_at = datetime.now(UTC)
            self.session.commit()
        return discovered

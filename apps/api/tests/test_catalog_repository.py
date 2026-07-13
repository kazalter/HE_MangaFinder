from datetime import UTC, datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models import Author, AuthorWork, Work, WorkSource
from app.modules.catalog.repository import CatalogRepository
from app.providers.base import DiscoveredWork


def test_upsert_is_idempotent_and_links_author() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        author = Author(name="Author")
        session.add(author)
        session.flush()
        repository = CatalogRepository(session)
        item = DiscoveredWork(
            external_id="external-1", title="First title", source_url="https://example.test/1"
        )
        first = repository.upsert(author.id, "test", item)
        repository.upsert(
            author.id,
            "test",
            DiscoveredWork(
                external_id="external-1",
                title="Updated title",
                source_url="https://example.test/1",
            ),
        )
        session.commit()

        assert session.scalar(select(Work).where(Work.id == first.id)).title == "Updated title"
        assert len(list(session.scalars(select(Work)))) == 1
        assert len(list(session.scalars(select(WorkSource)))) == 1
        assert len(list(session.scalars(select(AuthorWork)))) == 1


def test_lists_works_by_latest_source_date_with_undated_last() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        author = Author(name="Author")
        session.add(author)
        session.flush()
        repository = CatalogRepository(session)

        for external_id, title, updated_at in (
            ("old", "Old", datetime(2024, 1, 1, tzinfo=UTC)),
            ("undated", "Undated", None),
            ("new", "New", datetime(2026, 7, 13, tzinfo=UTC)),
        ):
            repository.upsert(
                author.id,
                "test",
                DiscoveredWork(
                    external_id=external_id,
                    title=title,
                    source_url=f"https://example.test/{external_id}",
                    source_updated_at=updated_at,
                ),
            )
        session.commit()

        rows = repository.list_for_author(author.id)

        assert [work.title for work, _ in rows] == ["New", "Old", "Undated"]

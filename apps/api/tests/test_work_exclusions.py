from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models import (
    Author,
    AuthorWork,
    AuthorWorkExclusion,
    Work,
    WorkGroup,
    WorkSource,
)
from app.modules.catalog.aggregation import AggregationService
from app.modules.catalog.exclusions import WorkExclusionService
from app.modules.catalog.repairs import remove_retired_provider_sources
from app.modules.catalog.repository import CatalogRepository
from app.providers.base import DiscoveredWork


def discovered(external_id: str = "bad-1") -> DiscoveredWork:
    return DiscoveredWork(
        external_id=external_id,
        title="[其他作者] 错误作品 [YD汉化]",
        source_url=f"https://example.test/{external_id}",
    )


def test_manual_exclusion_removes_orphan_and_survives_refresh() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        author = Author(name="YD")
        session.add(author)
        session.flush()
        repository = CatalogRepository(session)
        work = repository.upsert(author.id, "wnacg", discovered())
        AggregationService(session).assign_without_cover(work, author)
        session.flush()

        WorkExclusionService(session).exclude_work(author, work)
        session.commit()

        assert list(session.scalars(select(AuthorWork))) == []
        exclusion = session.scalar(select(AuthorWorkExclusion))
        assert exclusion is not None
        assert exclusion.external_id == "bad-1"
        assert repository.excluded_external_ids(author.id, "wnacg") == {"bad-1"}
        assert list(session.scalars(select(Work))) == []
        assert list(session.scalars(select(WorkGroup))) == []


def test_retired_mangadex_records_are_removed() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        author = Author(name="Legacy author")
        session.add(author)
        session.flush()
        work = CatalogRepository(session).upsert(
            author.id,
            "mangadex",
            DiscoveredWork(
                external_id="legacy-1",
                title="Legacy work",
                source_url="https://legacy.example/work",
            ),
        )
        AggregationService(session).assign_without_cover(work, author)
        session.commit()

        removed = remove_retired_provider_sources(session)

        assert removed == 1
        assert list(session.scalars(select(WorkSource))) == []
        assert list(session.scalars(select(Work))) == []
        assert list(session.scalars(select(WorkGroup))) == []
        assert list(session.scalars(select(AuthorWork))) == []


def test_manual_exclusion_keeps_work_linked_to_another_author() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        wrong_author = Author(name="YD")
        actual_author = Author(name="其他作者")
        session.add_all([wrong_author, actual_author])
        session.flush()
        repository = CatalogRepository(session)
        work = repository.upsert(wrong_author.id, "wnacg", discovered("shared"))
        repository.upsert(actual_author.id, "wnacg", discovered("shared"))
        AggregationService(session).assign_without_cover(work, actual_author)
        session.flush()

        WorkExclusionService(session).exclude_work(wrong_author, work)
        session.commit()

        assert session.get(Work, work.id) is not None
        assert session.get(AuthorWork, (wrong_author.id, work.id)) is None
        assert session.get(AuthorWork, (actual_author.id, work.id)) is not None
        assert len(list(session.scalars(select(WorkGroup)))) == 1

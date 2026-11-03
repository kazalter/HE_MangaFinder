from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import Author, AuthorWork, Work, WorkGroup, WorkGroupMember, WorkSource
from app.db.session import get_session
from app.modules.catalog.groups_router import router


def test_detail_distinguishes_work_year_source_time_and_discovery_time():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    source_time = datetime(2021, 2, 3, tzinfo=UTC)
    first_discovery = datetime(2024, 4, 5, tzinfo=UTC)
    second_discovery = datetime(2025, 6, 7, tzinfo=UTC)
    with Session(engine) as session:
        author = Author(name="Artist")
        first = Work(title="First", year=2019)
        second = Work(title="Second")
        group = WorkGroup(title="Group", year=2019, first_source_at=source_time)
        session.add_all([
            author,
            WorkGroupMember(group=group, work=first),
            WorkGroupMember(group=group, work=second),
            WorkSource(work=first, provider="test", external_id="1",
                       source_url="https://example.test", source_updated_at=source_time),
        ])
        session.flush()
        session.add_all([
            AuthorWork(author_id=author.id, work_id=first.id, discovered_at=first_discovery),
            AuthorWork(author_id=author.id, work_id=second.id, discovered_at=second_discovery),
        ])
        session.commit()
        group_id, first_id, second_id = group.id, first.id, second.id

    app = FastAPI()
    app.include_router(router, prefix="/api")

    def override():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override
    with TestClient(app) as client:
        response = client.get(f"/api/work-groups/{group_id}")
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["year"] == 2019
        assert result["first_source_at"].startswith("2021-02-03")
        assert result["first_discovered_at"].startswith("2024-04-05")
        editions = {item["work_id"]: item for item in result["editions"]}
        assert editions[first_id]["latest_source_at"].startswith("2021-02-03")
        assert editions[first_id]["first_discovered_at"].startswith("2024-04-05")
        assert editions[second_id]["first_discovered_at"].startswith("2025-06-07")

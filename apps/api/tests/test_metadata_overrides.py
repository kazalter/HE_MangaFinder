from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import Work, WorkFingerprint, WorkGroup, WorkGroupMember, WorkSource
from app.db.session import get_session
from app.modules.catalog.aggregation import AggregationService
from app.modules.catalog.groups_router import router


def test_manual_fields_preferred_version_and_split_are_preserved():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        first = Work(
            title="First",
            description="first description",
            cover_url="https://t.nhentai.net/galleries/1/cover.jpg",
            year=2020,
            language="ja",
            tags=["original"],
        )
        second = Work(
            title="Second",
            description="second description",
            cover_url="https://t.nhentai.net/galleries/2/cover.jpg",
            year=2022,
            language="en",
            tags=["english"],
        )
        group = WorkGroup(title="First", tags=[])
        session.add_all(
            [
                group,
                WorkGroupMember(group=group, work=first),
                WorkGroupMember(group=group, work=second),
                WorkFingerprint(work=first, normalized_title="first", page_count=20),
                WorkFingerprint(work=second, normalized_title="second", page_count=22),
                WorkSource(
                    work=first,
                    provider="nhentai",
                    external_id="1",
                    source_url="https://example.test/1",
                ),
                WorkSource(
                    work=second,
                    provider="nhentai",
                    external_id="2",
                    source_url="https://example.test/2",
                ),
            ]
        )
        session.commit()
        group_id, first_id, second_id = group.id, first.id, second.id

    app = FastAPI()
    app.include_router(router, prefix="/api")

    def override():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override
    with TestClient(app) as client:
        url = f"/api/work-groups/{group_id}/metadata"
        response = client.patch(
            url,
            json={
                "title": "My title",
                "year": 1999,
                "preferred_work_id": second_id,
                "cover_work_id": second_id,
            },
        )
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["title"] == "My title" and result["year"] == 1999
        assert result["editions"][0]["work_id"] == second_id
        assert result["editions"][0]["page_count"] == 22
        assert result["description"] == "second description"
        assert result["metadata_sources"]["title"]["locked"] is True
        assert result["metadata_sources"]["cover_url"]["edition_ids"] == [second_id]
        assert client.patch(url, json={"preferred_work_id": 999}).status_code == 422
        with Session(engine) as session:
            work = session.get(Work, first_id)
            work.title = "Updated from provider"
            group = session.get(WorkGroup, group_id)
            AggregationService(session).recompute(group)
            session.commit()
            assert group.title == "My title" and group.year == 1999
        result = client.patch(url, json={"title": None}).json()
        assert (
            result["title"] == "second" and result["metadata_sources"]["title"]["locked"] is False
        )
        response = client.post(f"/api/work-groups/{group_id}/members/{second_id}/split")
        assert response.status_code == 200, response.text
        split = response.json()
        assert split["metadata_overrides"]["cover_work_id"] == second_id
        assert split["metadata_overrides"]["preferred_work_id"] == second_id
        original = client.get(f"/api/work-groups/{group_id}").json()
        assert original["metadata_overrides"] == {"year": 1999}


def test_conflicting_manual_settings_block_merge_without_data_loss():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        first = Work(title="First")
        second = Work(title="Second")
        target = WorkGroup(title="One", manual_metadata={"title": "One"})
        source = WorkGroup(title="Two", manual_metadata={"title": "Two"})
        session.add_all(
            [WorkGroupMember(group=target, work=first), WorkGroupMember(group=source, work=second)]
        )
        session.commit()
        import pytest

        with pytest.raises(ValueError, match="手工设置"):
            AggregationService(session).merge_groups(target, source)
        session.rollback()
        assert session.get(WorkGroup, target.id) is not None
        assert session.get(WorkGroup, source.id) is not None

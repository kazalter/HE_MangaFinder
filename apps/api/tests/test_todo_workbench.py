from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models import (
    Author,
    CatalogEvent,
    Job,
    JobStatus,
    PlannedRelease,
    Work,
    WorkGroup,
    WorkGroupMember,
)
from app.modules.dashboard.repository import DashboardRepository
from app.modules.jobs.router import failed_downloads
from app.modules.social.router import list_planned_releases


def test_failed_downloads_only_include_latest_unresolved_attempt():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        work = Work(title="Test work")
        group = WorkGroup(title="Group")
        session.add_all([work, group])
        session.flush()
        session.add(WorkGroupMember(work_id=work.id, group_id=group.id))

        def job(chapter, status):
            return Job(
                kind="download_chapter",
                payload={"work_id": work.id, "provider": "p", "chapter_id": chapter},
                status=status,
            )

        session.add_all(
            [
                job("1", JobStatus.FAILED),
                job("1", JobStatus.SUCCEEDED),
                job("2", JobStatus.FAILED),
                job("3", JobStatus.FAILED),
                job("3", JobStatus.PENDING),
                job("4", JobStatus.FAILED),
            ]
        )
        session.commit()
        result = failed_downloads(session, limit=1)
        assert result["has_more"] is True
        assert len(result["items"]) == 1
        assert result["items"][0]["job"]["payload"]["chapter_id"] == "4"
        assert result["items"][0]["group_id"] == group.id
        result = failed_downloads(session, limit=100)
        assert [item["job"]["payload"]["chapter_id"] for item in result["items"]] == ["4", "2"]


def test_inbox_pagination_and_planned_match_filter():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        author = Author(name="Test")
        group = WorkGroup(title="Candidate")
        session.add_all([author, group])
        session.flush()
        session.add_all(
            [
                CatalogEvent(
                    author_id=author.id,
                    provider="test",
                    external_id=str(i),
                    event_type="work_discovered",
                    title=f"Work {i}",
                )
                for i in range(3)
            ]
        )
        session.add_all(
            [
                PlannedRelease(
                    author_id=author.id,
                    title="Candidate",
                    normalized_title="candidate",
                    status="discovered",
                    suggested_group_id=group.id,
                ),
                PlannedRelease(
                    author_id=author.id,
                    title="Announced",
                    normalized_title="announced",
                    status="announced",
                ),
            ]
        )
        session.commit()
        repo = DashboardRepository(session)
        first = repo.inbox(state="unread", author_id=None, limit=2, offset=0)
        second = repo.inbox(state="unread", author_id=None, limit=2, offset=2)
        assert len(first) == 2 and len(second) == 1
        assert not {e.id for e in first} & {e.id for e in second}
        assert repo.set_all_event_states("read", ids=[first[0].id]) == 1
        assert len(repo.inbox(state="unread", author_id=None, limit=10)) == 2
        assert [
            p.title
            for p in list_planned_releases(
                session, author_id=None, include_closed=False, needs_match=True, limit=100
            )
        ] == ["Candidate"]


def test_bulk_route_changes_only_selected_unread_events():
    from fastapi.testclient import TestClient
    from sqlalchemy.pool import StaticPool

    from app.db.session import get_session
    from app.main import app

    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        author = Author(name="Test")
        session.add(author)
        session.flush()
        session.add_all(
            [
                CatalogEvent(
                    author_id=author.id,
                    provider="test",
                    external_id=str(i),
                    event_type="work_discovered",
                    title=f"Work {i}",
                )
                for i in range(3)
            ]
        )
        session.commit()
        ids = [row.id for row in session.query(CatalogEvent).order_by(CatalogEvent.id)]

    def session_override():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = session_override
    client = TestClient(app)
    try:
        assert (
            client.post(
                "/api/system-config/auth/bootstrap", json={"password": "secure password 123"}
            ).status_code
            == 200
        )
        headers = {"X-CSRF-Token": client.cookies.get("mangafinder_csrf")}
        assert (
            client.post(
                "/api/discovery-inbox/bulk", json={"state": "read", "ids": []}, headers=headers
            ).status_code
            == 422
        )
        response = client.post(
            "/api/discovery-inbox/bulk", json={"state": "read", "ids": [ids[1]]}, headers=headers
        )
        assert response.status_code == 200 and response.json()["changed"] == 1
        unread = client.get("/api/discovery-inbox?state=unread").json()
        assert {row["id"] for row in unread} == {ids[0], ids[2]}
    finally:
        client.close()
        app.dependency_overrides.pop(get_session, None)

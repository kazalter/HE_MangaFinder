import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models import (
    Author,
    AuthorWork,
    AuthorWorkExclusion,
    CatalogEvent,
    CatalogOperation,
    Series,
    SeriesMember,
    Work,
    WorkFingerprint,
    WorkGroup,
    WorkGroupMember,
    WorkRelation,
    WorkSource,
)
from app.modules.catalog.aggregation import AggregationService
from app.modules.catalog.exclusions import WorkExclusionService
from app.modules.catalog.operation_history import UndoConflict, record_operation, undo_operation


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'history.db'}")

    @event.listens_for(engine, "connect")
    def foreign_keys(dbapi_connection, _):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session
    engine.dispose()


def catalog(session):
    author = Author(name="writer")
    first = Work(title="first", tags=[])
    second = Work(title="second", tags=[])
    left = WorkGroup(title="first", tags=[], manual_metadata={"title": "first"})
    right = WorkGroup(title="second", tags=[])
    series = Series(title="series", normalized_title="series")
    session.add_all(
        [
            author,
            first,
            second,
            left,
            right,
            series,
            WorkGroupMember(group=left, work=first),
            WorkGroupMember(group=right, work=second),
            WorkFingerprint(work=first, normalized_title="first"),
            WorkFingerprint(work=second, normalized_title="second"),
            WorkSource(
                work=first, provider="test", external_id="1", source_url="https://example.test/1"
            ),
            WorkSource(
                work=second, provider="test", external_id="2", source_url="https://example.test/2"
            ),
        ]
    )
    session.flush()
    session.add_all(
        [
            AuthorWork(author_id=author.id, work_id=first.id),
            AuthorWork(author_id=author.id, work_id=second.id),
            SeriesMember(series_id=series.id, group_id=right.id, volume_number=2),
            WorkRelation(source_group_id=left.id, target_group_id=right.id, relation_type="sequel"),
            CatalogEvent(
                author_id=author.id,
                group_id=right.id,
                work_id=second.id,
                provider="test",
                external_id="2",
                event_type="work_discovered",
                title="second",
            ),
        ]
    )
    session.commit()
    return author, first, second, left, right


def test_merge_undo_restores_cascaded_relations_and_metadata(session):
    _, first, second, target, source = catalog(session)
    source_id, target_id, second_id = source.id, target.id, second.id
    with record_operation(session, "merge", [target_id, source_id], "merge") as journal:
        AggregationService(session).merge_groups(target, source)
    session.commit()
    operation_id = journal.operation.id
    assert session.get(WorkGroup, source_id) is None
    assert session.scalars(select(WorkRelation)).first() is None
    undo_operation(session, operation_id)
    session.commit()
    session.expire_all()
    assert session.get(WorkGroup, source_id).title == "second"
    assert session.get(WorkGroup, target_id).manual_metadata == {"title": "first"}
    assert session.get(Work, second_id).group_membership.group_id == source_id
    assert session.scalars(select(WorkRelation)).one().target_group_id == source_id
    assert session.scalars(select(SeriesMember)).one().group_id == source_id
    assert session.scalars(select(CatalogEvent)).one().group_id == source_id
    assert session.get(CatalogOperation, operation_id).undone_at is not None


def test_undo_rejects_later_edits_atomically(session):
    _, _, _, target, source = catalog(session)
    source_id, target_id = source.id, target.id
    with record_operation(session, "merge", [target_id, source_id], "merge") as journal:
        AggregationService(session).merge_groups(target, source)
    session.commit()
    operation_id = journal.operation.id
    target.title = "edited afterwards"
    session.commit()
    with pytest.raises(UndoConflict):
        undo_operation(session, operation_id)
    session.rollback()
    assert session.get(WorkGroup, source_id) is None
    assert session.get(WorkGroup, target_id).title == "edited afterwards"
    assert session.get(CatalogOperation, operation_id).undone_at is None


def test_exclusion_undo_restores_work_and_removes_exclusion(session):
    author, _, work, _, group = catalog(session)
    author_id, work_id, group_id = author.id, work.id, group.id
    with record_operation(session, "exclude", [group_id], "exclude") as journal:
        WorkExclusionService(session).exclude_work(author, work)
    session.commit()
    operation_id = journal.operation.id
    assert session.get(Work, work_id) is None
    assert session.scalars(select(AuthorWorkExclusion)).first() is not None
    undo_operation(session, operation_id)
    session.commit()
    session.expire_all()
    assert session.get(Work, work_id) is not None
    assert session.get(AuthorWork, (author_id, work_id)) is not None
    assert session.get(WorkGroup, group_id) is not None
    assert session.scalars(select(AuthorWorkExclusion)).first() is None
    assert session.scalars(select(CatalogEvent)).one().work_id == work_id


def test_undo_split_refuses_to_remove_new_references(session):
    author, _, second, target, source = catalog(session)
    AggregationService(session).merge_groups(target, source)
    session.commit()
    target_id, work_id = target.id, second.id
    with record_operation(session, "split", [target_id], "split") as journal:
        created = AggregationService(session).split_member(target, work_id)
        journal.operation.group_ids = [target_id, created.id]
    session.commit()
    operation_id, new_group_id = journal.operation.id, created.id
    session.add(
        CatalogEvent(
            author_id=author.id,
            group_id=new_group_id,
            work_id=work_id,
            provider="test",
            external_id="new",
            event_type="edition_discovered",
            title="new",
        )
    )
    session.commit()
    with pytest.raises(UndoConflict, match="关联已变化"):
        undo_operation(session, operation_id)
    session.rollback()
    assert session.get(WorkGroup, new_group_id) is not None
    assert session.get(CatalogOperation, operation_id).undone_at is None


def test_failed_operation_rolls_back_temporary_journal_and_edits(session):
    _, _, _, target, source = catalog(session)
    with pytest.raises(ValueError):
        with record_operation(session, "merge", [target.id, source.id], "merge"):
            target.title = "temporary"
            session.flush()
            raise ValueError("stop")
    assert session.scalars(select(CatalogOperation)).first() is None
    assert session.get(WorkGroup, target.id).title == "first"
    assert (
        session.connection()
        .exec_driver_sql("SELECT name FROM sqlite_temp_master WHERE name LIKE '_mf_%'")
        .fetchall()
        == []
    )
    with record_operation(session, "merge", [target.id, source.id], "merge"):
        AggregationService(session).merge_groups(target, source)
    session.commit()
    assert session.scalars(select(CatalogOperation)).one().kind == "merge"


def test_manual_merge_endpoint_records_and_undoes_without_exposing_rows(session):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.db.session import get_session
    from app.modules.catalog.groups_router import router as groups_router
    from app.modules.catalog.operations_router import router as operations_router

    author, _, _, target, source = catalog(session)
    target_id, source_id, author_id = target.id, source.id, author.id
    engine = session.get_bind()
    app = FastAPI()
    app.include_router(groups_router, prefix="/api")
    app.include_router(operations_router, prefix="/api")

    def override():
        with Session(engine) as request_session:
            yield request_session

    app.dependency_overrides[get_session] = override
    with TestClient(app) as client:
        response = client.post(
            f"/api/work-groups/{target_id}/merge", json={"source_group_id": source_id}
        )
        assert response.status_code == 200, response.text
        records = client.get("/api/catalog-operations").json()
        assert len(records) == 1 and records[0]["kind"] == "merge"
        assert "changes" not in records[0]
        operation_id = records[0]["id"]
        assert client.post(f"/api/catalog-operations/{operation_id}/undo").status_code == 200
        assert client.get(f"/api/work-groups/{source_id}").status_code == 200
        assert client.post(f"/api/catalog-operations/{operation_id}/undo").status_code == 409
        assert client.delete(f"/api/work-groups/{source_id}/authors/{author_id}").status_code == 204
        exclusion = client.get("/api/catalog-operations").json()[0]
        assert exclusion["before_group_ids"] == [source_id]
        assert exclusion["after_group_ids"] == []
        assert client.post(f"/api/catalog-operations/{exclusion['id']}/undo").status_code == 200
        assert client.get(f"/api/work-groups/{source_id}").status_code == 200


def test_split_and_exclusion_endpoints_restore_all_data(session):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.db.session import get_session
    from app.modules.catalog.groups_router import router as groups_router
    from app.modules.catalog.operations_router import router as operations_router

    author, _, second, target, source = catalog(session)
    author_id, work_id, group_id = author.id, second.id, target.id
    AggregationService(session).merge_groups(target, source)
    session.commit()
    engine = session.get_bind()
    app = FastAPI()
    app.include_router(groups_router, prefix="/api")
    app.include_router(operations_router, prefix="/api")

    def override():
        with Session(engine) as request_session:
            yield request_session

    app.dependency_overrides[get_session] = override
    with TestClient(app) as client:
        response = client.post(f"/api/work-groups/{group_id}/members/{work_id}/split")
        assert response.status_code == 200, response.text
        new_group_id = response.json()["id"]
        operation = client.get("/api/catalog-operations").json()[0]
        assert operation["before_group_ids"] == [group_id]
        assert operation["after_group_ids"] == [group_id, new_group_id]
        assert client.post(f"/api/catalog-operations/{operation['id']}/undo").status_code == 200
        assert client.get(f"/api/work-groups/{new_group_id}").status_code == 404
        response = client.delete(
            f"/api/work-groups/{group_id}/members/{work_id}/authors/{author_id}"
        )
        assert response.status_code == 204, response.text
        operation = client.get("/api/catalog-operations").json()[0]
        assert operation["kind"] == "exclude"
        assert client.post(f"/api/catalog-operations/{operation['id']}/undo").status_code == 200
        result = client.get(f"/api/work-groups/{group_id}").json()
        assert work_id in [edition["work_id"] for edition in result["editions"]]


def test_merge_restores_candidate_review_and_planned_release(session):
    from app.db.models import AgentReview, MergeSuggestion, PlannedRelease

    author, _, _, target, source = catalog(session)
    suggestion = MergeSuggestion(
        source_group_id=source.id, target_group_id=target.id, confidence=0.9, reasons=[]
    )
    plan = PlannedRelease(
        author_id=author.id,
        title="upcoming",
        normalized_title="upcoming",
        linked_group_id=source.id,
        suggested_group_id=source.id,
    )
    session.add_all([suggestion, plan])
    session.flush()
    review = AgentReview(
        suggestion_id=suggestion.id,
        candidate_key="k",
        evidence_hash="h",
        provider="test",
        model="test",
        prompt_version="1",
        schema_version="1",
        status="succeeded",
    )
    session.add(review)
    session.commit()
    suggestion_id, review_id, plan_id, source_id = suggestion.id, review.id, plan.id, source.id
    with record_operation(session, "merge", [target.id, source.id], "merge") as journal:
        AggregationService(session).merge_groups(target, source)
    session.commit()
    undo_operation(session, journal.operation.id)
    session.commit()
    session.expire_all()
    assert session.get(MergeSuggestion, suggestion_id) is not None
    assert session.get(AgentReview, review_id).suggestion_id == suggestion_id
    restored_plan = session.get(PlannedRelease, plan_id)
    assert restored_plan.linked_group_id == source_id
    assert restored_plan.suggested_group_id == source_id


def test_merge_reads_catalog_after_acquiring_write_lock(session):
    from concurrent.futures import ThreadPoolExecutor
    from time import sleep

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.db.session import get_session
    from app.modules.catalog.groups_router import router as groups_router
    from app.modules.catalog.operations_router import router as operations_router

    _, _, _, target, source = catalog(session)
    target_id, source_id = target.id, source.id
    engine = session.get_bind()
    app = FastAPI()
    app.include_router(groups_router, prefix="/api")
    app.include_router(operations_router, prefix="/api")

    def override():
        with Session(engine) as request_session:
            yield request_session

    app.dependency_overrides[get_session] = override
    with TestClient(app) as client, engine.connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        connection.exec_driver_sql(
            "UPDATE work_groups SET title=? WHERE id=?", ("new title", target_id)
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                client.post,
                f"/api/work-groups/{target_id}/merge",
                json={"source_group_id": source_id},
            )
            sleep(0.2)
            assert not future.done()
            connection.commit()
            response = future.result(timeout=5)
        assert response.status_code == 200, response.text
        assert "new title" in client.get("/api/catalog-operations").json()[0]["summary"]


def test_undo_merge_rejects_new_reference_to_existing_target(session):
    author, _, moved_work, target, source = catalog(session)
    source_id, target_id, work_id = source.id, target.id, moved_work.id
    with record_operation(session, "merge", [target_id, source_id], "merge") as journal:
        AggregationService(session).merge_groups(target, source)
    session.commit()
    operation_id = journal.operation.id
    session.add(
        CatalogEvent(
            author_id=author.id,
            group_id=target_id,
            work_id=work_id,
            provider="test",
            external_id="later",
            event_type="edition_discovered",
            title="later",
        )
    )
    session.commit()
    with pytest.raises(UndoConflict, match="关联已变化"):
        undo_operation(session, operation_id)
    session.rollback()
    assert session.get(WorkGroup, source_id) is None
    assert session.get(CatalogOperation, operation_id).undone_at is None

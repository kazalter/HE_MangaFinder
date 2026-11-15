"""Author-level aggregation invalidation and revision tracking."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session
from sqlalchemy.orm.util import identity_key as orm_identity_key

from app.core.time import as_utc
from app.db.models import (
    Author,
    AuthorAggregationState,
    AuthorWork,
    WorkGroupMember,
)

AGGREGATION_ALGORITHM_VERSION = "agg-v4"


def current_algorithm_version() -> str:
    return AGGREGATION_ALGORITHM_VERSION


def _utcnow(now: datetime | None) -> datetime:
    return as_utc(now) if now is not None else datetime.now(UTC)


def _pending_state(session: Session, author_id: int) -> AuthorAggregationState | None:
    for obj in session.new:
        if isinstance(obj, AuthorAggregationState) and obj.author_id == author_id:
            return obj
    return None


def _loaded_state(session: Session, author_id: int) -> AuthorAggregationState | None:
    pending = _pending_state(session, author_id)
    if pending is not None:
        return pending
    key = orm_identity_key(AuthorAggregationState, (author_id,))
    obj = session.identity_map.get(key)
    return obj if isinstance(obj, AuthorAggregationState) else None


def authors_for_works(session: Session, work_ids: Iterable[int]) -> set[int]:
    ids = {int(work_id) for work_id in work_ids if work_id is not None}
    if not ids:
        return set()
    deleted_pairs = {
        (obj.author_id, obj.work_id)
        for obj in session.deleted
        if isinstance(obj, AuthorWork)
    }
    author_ids = {
        row[0]
        for row in session.execute(
            select(AuthorWork.author_id, AuthorWork.work_id).where(AuthorWork.work_id.in_(ids))
        ).all()
        if (row[0], row[1]) not in deleted_pairs
    }
    for obj in session.new:
        if isinstance(obj, AuthorWork) and obj.work_id in ids:
            author_ids.add(obj.author_id)
    return author_ids


def authors_for_groups(session: Session, group_ids: Iterable[int]) -> set[int]:
    ids = {int(group_id) for group_id in group_ids if group_id is not None}
    if not ids:
        return set()
    deleted_members = {
        obj.work_id for obj in session.deleted if isinstance(obj, WorkGroupMember)
    }
    rows = session.execute(
        select(AuthorWork.author_id, WorkGroupMember.work_id)
        .join(WorkGroupMember, WorkGroupMember.work_id == AuthorWork.work_id)
        .where(WorkGroupMember.group_id.in_(ids))
    ).all()
    author_ids = {author_id for author_id, work_id in rows if work_id not in deleted_members}
    pending_work_ids = {
        obj.work_id
        for obj in session.new
        if isinstance(obj, WorkGroupMember) and obj.group_id in ids and obj.work_id is not None
    }
    if pending_work_ids:
        author_ids.update(authors_for_works(session, pending_work_ids))
    return author_ids


def bump_author_aggregation(
    session: Session,
    author_id: int,
    *,
    reason: str,
    full_reconcile: bool = True,
    exclude_author_id: int | None = None,
    now: datetime | None = None,
) -> bool:
    """Advance `input_revision` for one author so future aggregation runs reconcile it."""
    if exclude_author_id is not None and author_id == exclude_author_id:
        return False
    ts = _utcnow(now)
    reason_text = reason[:120]
    pending = _pending_state(session, author_id)
    if pending is not None:
        pending.input_revision = (pending.input_revision or 0) + 1
        if full_reconcile:
            pending.needs_full_reconcile = True
        pending.dirty_reason = reason_text
        pending.updated_at = ts
        return True

    values: dict[str, Any] = {
        "input_revision": AuthorAggregationState.input_revision + 1,
        "dirty_reason": reason_text,
        "updated_at": ts,
    }
    if full_reconcile:
        values["needs_full_reconcile"] = True

    res = session.execute(
        update(AuthorAggregationState)
        .where(AuthorAggregationState.author_id == author_id)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    loaded = _loaded_state(session, author_id)
    if res.rowcount > 0:
        if loaded is not None:
            session.expire(loaded)
        return True

    author_exists = (
        session.scalar(select(Author.id).where(Author.id == author_id)) is not None
        or any(
            isinstance(obj, Author) and obj.id == author_id
            for obj in session.new
        )
    )
    if not author_exists:
        return False

    session.add(
        AuthorAggregationState(
            author_id=author_id,
            input_revision=1,
            completed_revision=0,
            needs_full_reconcile=full_reconcile,
            algorithm_version=None,
            dirty_reason=reason_text,
            updated_at=ts,
        )
    )
    return True


def bump_authors_for_works(
    session: Session,
    work_ids: Iterable[int],
    *,
    reason: str,
    full_reconcile: bool = True,
    extra_author_ids: Iterable[int] = (),
    exclude_author_id: int | None = None,
    now: datetime | None = None,
) -> set[int]:
    targets = authors_for_works(session, work_ids) | set(extra_author_ids)
    bumped: set[int] = set()
    for author_id in sorted(targets):
        if bump_author_aggregation(
            session,
            author_id,
            reason=reason,
            full_reconcile=full_reconcile,
            exclude_author_id=exclude_author_id,
            now=now,
        ):
            bumped.add(author_id)
    return bumped


def bump_authors_for_groups(
    session: Session,
    group_ids: Iterable[int],
    *,
    reason: str,
    full_reconcile: bool = True,
    extra_author_ids: Iterable[int] = (),
    exclude_author_id: int | None = None,
    now: datetime | None = None,
) -> set[int]:
    targets = authors_for_groups(session, group_ids) | set(extra_author_ids)
    bumped: set[int] = set()
    for author_id in sorted(targets):
        if bump_author_aggregation(
            session,
            author_id,
            reason=reason,
            full_reconcile=full_reconcile,
            exclude_author_id=exclude_author_id,
            now=now,
        ):
            bumped.add(author_id)
    return bumped


def _read_state(session: Session, author_id: int) -> AuthorAggregationState | None:
    session.flush()
    loaded = _loaded_state(session, author_id)
    if loaded is not None:
        session.expire(loaded)
    return session.get(AuthorAggregationState, author_id)


def needs_author_aggregation(
    session: Session,
    author_id: int,
    *,
    force: bool = False,
    algorithm_version: str | None = None,
) -> bool:
    """Return True if author has pending input/algorithm/ungrouped changes."""
    if force:
        return True
    algo = algorithm_version or current_algorithm_version()
    state = _read_state(session, author_id)
    if state is None:
        return True
    if state.completed_revision < state.input_revision:
        return True
    if state.algorithm_version != algo:
        return True
    ungrouped = session.scalar(
        select(AuthorWork.work_id)
        .outerjoin(WorkGroupMember, WorkGroupMember.work_id == AuthorWork.work_id)
        .where(
            AuthorWork.author_id == author_id,
            WorkGroupMember.work_id.is_(None),
        )
        .limit(1)
    )
    return ungrouped is not None


def should_full_reconcile(
    session: Session,
    author_id: int,
    *,
    force: bool = False,
    algorithm_version: str | None = None,
) -> bool:
    if force:
        return True
    algo = algorithm_version or current_algorithm_version()
    state = _read_state(session, author_id)
    if state is None:
        return True
    if state.needs_full_reconcile:
        return True
    if state.algorithm_version != algo:
        return True
    return False


def capture_author_revision(
    session: Session,
    author_id: int,
    *,
    reason: str = "aggregation_run",
    now: datetime | None = None,
) -> int:
    """Ensure a state row exists and capture the current `input_revision` without committing."""
    ts = _utcnow(now)
    state = _read_state(session, author_id)
    if state is None:
        state = AuthorAggregationState(
            author_id=author_id,
            input_revision=1,
            completed_revision=0,
            needs_full_reconcile=True,
            algorithm_version=None,
            dirty_reason=reason[:120],
            updated_at=ts,
        )
        session.add(state)
        session.flush()
    return int(state.input_revision)


def complete_author_revision(
    session: Session,
    author_id: int,
    snapshot_revision: int,
    *,
    algorithm_version: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Mark `snapshot_revision` completed ONLY if no newer input revision arrived meanwhile."""
    session.flush()
    algo = algorithm_version or current_algorithm_version()
    ts = _utcnow(now)
    res = session.execute(
        update(AuthorAggregationState)
        .where(
            AuthorAggregationState.author_id == author_id,
            AuthorAggregationState.input_revision == snapshot_revision,
        )
        .values(
            completed_revision=snapshot_revision,
            needs_full_reconcile=False,
            algorithm_version=algo,
            dirty_reason=None,
            last_aggregated_at=ts,
            updated_at=ts,
        )
        .execution_options(synchronize_session=False)
    )
    loaded = _loaded_state(session, author_id)
    if loaded is not None:
        session.expire(loaded)
    return bool(res.rowcount and res.rowcount > 0)


def extract_author_ids_from_operation(
    session: Session,
    changes: list[dict[str, Any]],
    group_ids: Iterable[int] = (),
) -> set[int]:
    """Collect author IDs directly or indirectly referenced by a CatalogOperation."""
    direct_authors: set[int] = set()
    work_ids: set[int] = set()
    related_group_ids: set[int] = {int(gid) for gid in group_ids if gid is not None}

    for entry in changes:
        table = entry.get("table")
        for side in ("before", "after"):
            row = entry.get(side)
            if not isinstance(row, dict):
                continue
            if table in {"author_works", "author_work_exclusions"}:
                if row.get("author_id") is not None:
                    direct_authors.add(int(row["author_id"]))
                if row.get("work_id") is not None:
                    work_ids.add(int(row["work_id"]))
            elif table == "works" and row.get("id") is not None:
                work_ids.add(int(row["id"]))
            elif table == "work_groups" and row.get("id") is not None:
                related_group_ids.add(int(row["id"]))
            elif table == "work_group_members":
                if row.get("group_id") is not None:
                    related_group_ids.add(int(row["group_id"]))
                if row.get("work_id") is not None:
                    work_ids.add(int(row["work_id"]))

    return (
        direct_authors
        | authors_for_works(session, work_ids)
        | authors_for_groups(session, related_group_ids)
    )

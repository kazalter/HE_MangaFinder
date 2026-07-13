from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Work, WorkGroup, WorkSource
from app.modules.catalog.aggregation import AggregationService


def repair_wnacg_upload_years(session: Session) -> int:
    """Clear legacy Work.year values copied from WNACG upload timestamps."""
    rows = session.execute(
        select(Work, WorkSource)
        .join(WorkSource, WorkSource.work_id == Work.id)
        .where(
            WorkSource.provider == "wnacg",
            Work.year.is_not(None),
            WorkSource.source_updated_at.is_not(None),
        )
    ).all()
    affected_group_ids: set[int] = set()
    repaired = 0
    for work, source in rows:
        uploaded_at = source.source_updated_at
        metadata = source.raw_metadata or {}
        if (
            uploaded_at is None
            or work.year != uploaded_at.year
            or metadata.get("upload_year_repaired") is True
        ):
            continue
        work.year = None
        source.raw_metadata = {
            **metadata,
            "uploaded_at": uploaded_at.isoformat(),
            "upload_year_repaired": True,
        }
        if work.group_membership:
            affected_group_ids.add(work.group_membership.group_id)
        repaired += 1

    session.flush()
    aggregation = AggregationService(session)
    for group_id in affected_group_ids:
        group = session.get(WorkGroup, group_id)
        if group:
            aggregation.recompute(group)
    session.commit()
    return repaired

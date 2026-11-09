from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Job, Work, WorkGroupMember
from app.db.session import get_session
from app.modules.catalog.downloads import safe_download_path
from app.modules.jobs.repository import JobRepository
from app.modules.jobs.schemas import JobRead

router = APIRouter(prefix="/jobs", tags=["jobs"])
SessionDep = Annotated[Session, Depends(get_session)]
LimitQuery = Annotated[int, Query(ge=1, le=100)]


@router.get("", response_model=list[JobRead])
def list_jobs(
    session: SessionDep,
    limit: LimitQuery = 20,
    include_internal: bool = True,
) -> list[JobRead]:
    return [
        JobRead.model_validate(job)
        for job in JobRepository(session).list_recent(limit, include_internal=include_internal)
    ]


@router.get("/failed-downloads")
def failed_downloads(session: SessionDep, limit: LimitQuery = 100) -> dict[str, object]:
    # A later success or active retry resolves an earlier failure for the same edition.
    seen: set[tuple[str, str, str]] = set()
    failures: list[Job] = []
    rows = session.scalars(
        select(Job).where(Job.kind == "download_chapter").order_by(Job.id.desc())
    ).yield_per(200)
    for job in rows:
        payload = job.payload or {}
        key = tuple(str(payload.get(field, "")) for field in ("work_id", "provider", "chapter_id"))
        if not all(key) or key in seen:
            continue
        seen.add(key)
        if job.status == "failed":
            failures.append(job)
            if len(failures) > limit:
                break
    has_more = len(failures) > limit
    failures = failures[:limit]
    ids = {
        int(job.payload["work_id"])
        for job in failures
        if str(job.payload.get("work_id", "")).isdigit()
    }
    metadata = {
        work.id: (work.title, group_id)
        for work, group_id in session.execute(
            select(Work, WorkGroupMember.group_id)
            .outerjoin(WorkGroupMember, WorkGroupMember.work_id == Work.id)
            .where(Work.id.in_(ids))
        )
    }
    return {
        "items": [
            {
                "job": JobRead.model_validate(job).model_dump(),
                "work_title": metadata.get(int(job.payload["work_id"]), (None, None))[0]
                if str(job.payload.get("work_id", "")).isdigit()
                else None,
                "group_id": metadata.get(int(job.payload["work_id"]), (None, None))[1]
                if str(job.payload.get("work_id", "")).isdigit()
                else None,
            }
            for job in failures
        ],
        "has_more": has_more,
    }


@router.get("/{job_id}/file", response_class=FileResponse)
def download_job_file(job_id: int, session: SessionDep) -> FileResponse:
    job = session.get(Job, job_id)
    path_value = job.payload.get("output_path") if job else None
    path = safe_download_path(str(path_value), get_settings()) if path_value else None
    if path is None:
        raise HTTPException(status_code=404, detail="下载文件尚未就绪")
    return FileResponse(path, filename=path.name, media_type="application/vnd.comicbook+zip")

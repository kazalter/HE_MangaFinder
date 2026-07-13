from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Job
from app.db.session import get_session
from app.modules.catalog.downloads import safe_download_path
from app.modules.jobs.repository import JobRepository
from app.modules.jobs.schemas import JobRead

router = APIRouter(prefix="/jobs", tags=["jobs"])
SessionDep = Annotated[Session, Depends(get_session)]
LimitQuery = Annotated[int, Query(ge=1, le=100)]


@router.get("", response_model=list[JobRead])
def list_jobs(session: SessionDep, limit: LimitQuery = 20) -> list[JobRead]:
    return [JobRead.model_validate(job) for job in JobRepository(session).list_recent(limit)]


@router.get("/{job_id}/file", response_class=FileResponse)
def download_job_file(job_id: int, session: SessionDep) -> FileResponse:
    job = session.get(Job, job_id)
    path_value = job.payload.get("output_path") if job else None
    path = safe_download_path(str(path_value), get_settings()) if path_value else None
    if path is None:
        raise HTTPException(status_code=404, detail="下载文件尚未就绪")
    return FileResponse(path, filename=path.name, media_type="application/vnd.comicbook+zip")

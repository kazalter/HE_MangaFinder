from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.db.session import get_session
from app.modules.catalog.repository import CatalogRepository
from app.modules.catalog.schemas import (
    ChapterRead,
    DownloadCreate,
    WorkRead,
    WorkSourceRead,
)
from app.modules.jobs.repository import JobRepository
from app.modules.jobs.schemas import JobRead
from app.providers.base import ProviderCapability
from app.providers.registry import ProviderRegistry

router = APIRouter(prefix="/works", tags=["works"])
SessionDep = Annotated[Session, Depends(get_session)]


@router.get("", response_model=list[WorkRead])
def list_works(session: SessionDep, author_id: int | None = None) -> list[WorkRead]:
    rows = CatalogRepository(session).list_for_author(author_id)
    return [
        WorkRead(
            id=work.id,
            title=work.title,
            description=work.description,
            cover_url=work.cover_url,
            status=work.status,
            year=work.year,
            language=work.language,
            tags=work.tags or [],
            discovered_at=discovered_at,
            sources=[
                WorkSourceRead(
                    provider=source.provider,
                    external_id=source.external_id,
                    source_url=source.source_url,
                    source_updated_at=source.source_updated_at,
                )
                for source in work.sources
            ],
        )
        for work, discovered_at in rows
    ]


@router.get("/{work_id}/chapters", response_model=list[ChapterRead])
async def list_chapters(
    work_id: int, provider: str, request: Request, session: SessionDep
) -> list[ChapterRead]:
    source = CatalogRepository(session).get_source(work_id, provider)
    if source is None:
        raise HTTPException(status_code=404, detail="作品来源不存在")
    registry: ProviderRegistry = request.app.state.providers
    source_provider = registry.get(provider)
    if ProviderCapability.CHAPTER_LIST not in source_provider.capabilities:
        raise HTTPException(status_code=409, detail="该来源不提供章节列表")
    chapters = await source_provider.list_chapters(source.external_id)
    return [ChapterRead(**asdict(chapter)) for chapter in chapters]


@router.post(
    "/{work_id}/downloads", response_model=JobRead, status_code=status.HTTP_202_ACCEPTED
)
def download_chapter(
    work_id: int, payload: DownloadCreate, request: Request, session: SessionDep
) -> JobRead:
    source = CatalogRepository(session).get_source(work_id, payload.provider)
    if source is None:
        raise HTTPException(status_code=404, detail="作品来源不存在")
    registry: ProviderRegistry = request.app.state.providers
    source_provider = registry.get(payload.provider)
    if ProviderCapability.DOWNLOAD not in source_provider.capabilities:
        raise HTTPException(status_code=409, detail="该来源不支持下载")
    job = JobRepository(session).enqueue_download(
        work_id, payload.provider, payload.chapter_id
    )
    session.commit()
    return JobRead.model_validate(job)

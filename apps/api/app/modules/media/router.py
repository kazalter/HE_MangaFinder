from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.db.session import get_session
from app.modules.catalog.repository import CatalogRepository
from app.modules.media.service import CoverCacheService
from app.providers.errors import ProviderError

router = APIRouter(prefix="/works", tags=["media"])
SessionDep = Annotated[Session, Depends(get_session)]
_SUPPORTED_PROVIDERS = frozenset({"mangadex", "nhentai", "wnacg"})


@router.get("/{work_id}/cover", response_class=FileResponse)
async def work_cover(
    work_id: int, request: Request, session: SessionDep
) -> FileResponse:
    work = CatalogRepository(session).get(work_id)
    if work is None or not work.cover_url:
        raise HTTPException(status_code=404, detail="作品封面不存在")
    source = next(
        (
            item
            for item in work.sources
            if item.provider in _SUPPORTED_PROVIDERS
        ),
        None,
    )
    if source is None:
        raise HTTPException(status_code=409, detail="该历史来源不支持封面缓存")
    service: CoverCacheService = request.app.state.cover_cache
    try:
        cached = await service.get(source.provider, source.external_id, work.cover_url)
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return FileResponse(
        cached.path,
        media_type=cached.content_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )

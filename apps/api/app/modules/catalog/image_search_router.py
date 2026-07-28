from io import BytesIO
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.session import get_session
from app.modules.catalog.cover_fingerprint import fingerprint_image
from app.modules.catalog.image_search import LocalImageSearch

router = APIRouter(prefix="/image-search", tags=["image-search"])
SessionDep = Annotated[Session, Depends(get_session)]
_MAX_UPLOAD_BYTES = 10 * 1024 * 1024
_MAX_IMAGE_PIXELS = 40_000_000


class ImageSearchMatchRead(BaseModel):
    group_id: int
    group_title: str
    work_id: int
    edition_title: str
    cover_url: str | None
    distance: int
    mode: str
    confidence: str
    evidence: list[str]


@router.post("", response_model=list[ImageSearchMatchRead])
async def search_local_covers(
    request: Request,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> list[ImageSearchMatchRead]:
    content_type = request.headers.get("content-type", "").partition(";")[0].strip()
    if not content_type.startswith("image/"):
        raise HTTPException(status_code=415, detail="请直接上传图片文件")
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="图片不能超过 10 MB")
    data = await request.body()
    if not data or len(data) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="图片为空或超过 10 MB")
    try:
        with Image.open(BytesIO(data)) as source:
            if source.width * source.height > _MAX_IMAGE_PIXELS:
                raise HTTPException(status_code=413, detail="图片像素尺寸过大")
            source.load()
            query = fingerprint_image(source)
    except HTTPException:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="无法读取该图片") from exc

    return [
        ImageSearchMatchRead(
            group_id=item.group_id,
            group_title=item.group_title,
            work_id=item.work_id,
            edition_title=item.edition_title,
            cover_url=(f"/api/works/{item.work_id}/cover" if item.cover_url else None),
            distance=item.distance,
            mode=item.mode,
            confidence=item.confidence,
            evidence=list(item.evidence),
        )
        for item in LocalImageSearch(session).search(query, limit=limit)
    ]

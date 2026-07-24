from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import SocialAccount
from app.db.session import get_session
from app.modules.authors.repository import AuthorRepository
from app.modules.authors.schemas import AuthorCreate, AuthorRead
from app.modules.jobs.repository import JobRepository
from app.modules.jobs.schemas import JobRead
from app.modules.social.media_archive import SocialAvatarArchive

router = APIRouter(prefix="/authors", tags=["authors"])
SessionDep = Annotated[Session, Depends(get_session)]


@router.get("", response_model=list[AuthorRead])
def list_authors(session: SessionDep) -> list[AuthorRead]:
    rows = AuthorRepository(session).list_with_counts()
    return [
        AuthorRead.model_validate(author).model_copy(
            update={
                "work_count": count,
                "avatar_url": f"/api/authors/{author.id}/avatar" if avatar_url else None,
                "x_handle": x_handle,
                "x_display_name": x_display_name,
                "x_last_synced_at": x_last_synced_at,
                "x_sync_error": x_sync_error,
            }
        )
        for (
            author,
            count,
            avatar_url,
            x_handle,
            x_display_name,
            x_last_synced_at,
            x_sync_error,
        ) in rows
    ]


@router.get("/{author_id}/avatar", response_class=FileResponse)
async def author_avatar(author_id: int, session: SessionDep) -> Response:
    source_url = session.scalar(
        select(SocialAccount.avatar_url)
        .where(
            SocialAccount.author_id == author_id,
            SocialAccount.platform == "x",
            SocialAccount.status == "confirmed",
            SocialAccount.avatar_url.is_not(None),
        )
        .order_by(SocialAccount.created_at, SocialAccount.id)
        .limit(1)
    )
    if not source_url:
        raise HTTPException(status_code=404, detail="作者头像不存在")
    path = await SocialAvatarArchive(get_settings()).archive(source_url)
    if not path:
        raise HTTPException(status_code=502, detail="作者头像暂时无法归档")
    return FileResponse(
        path,
        media_type="image/webp",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.post("", response_model=AuthorRead, status_code=status.HTTP_201_CREATED)
def create_author(payload: AuthorCreate, session: SessionDep) -> AuthorRead:
    authors = AuthorRepository(session)
    if authors.find_by_name(payload.name):
        raise HTTPException(status_code=409, detail="该作者已在订阅列表中")
    author = authors.add(payload.name)
    JobRepository(session).enqueue_discovery(author.id, trigger="initial")
    session.commit()
    return AuthorRead.model_validate(author)


@router.post("/{author_id}/refresh", response_model=JobRead, status_code=status.HTTP_202_ACCEPTED)
def refresh_author(author_id: int, session: SessionDep) -> JobRead:
    if not AuthorRepository(session).get(author_id):
        raise HTTPException(status_code=404, detail="作者不存在")
    job = JobRepository(session).enqueue_discovery(author_id)
    session.commit()
    return JobRead.model_validate(job)


@router.delete("/{author_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_author(author_id: int, session: SessionDep) -> Response:
    repository = AuthorRepository(session)
    author = repository.get(author_id)
    if not author:
        raise HTTPException(status_code=404, detail="作者不存在")
    repository.delete(author)
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)

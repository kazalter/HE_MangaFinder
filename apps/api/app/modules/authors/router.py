from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import SocialAccount
from app.db.session import get_session
from app.modules.authors.repository import AuthorRepository
from app.modules.authors.schemas import (
    AuthorCreate,
    AuthorIdentityUpdate,
    AuthorQueryNameRead,
    AuthorRead,
)
from app.modules.catalog.repository import CatalogRepository
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


@router.get(
    "/{author_id}/query-name/{provider}", response_model=AuthorQueryNameRead
)
def author_query_name(
    author_id: int, provider: str, session: SessionDep
) -> AuthorQueryNameRead:
    author = AuthorRepository(session).get(author_id)
    if not author:
        raise HTTPException(status_code=404, detail="作者不存在")
    decision = CatalogRepository(session).author_query_name_decision(
        author.id, provider, author.name
    )
    return AuthorQueryNameRead(
        provider=provider,
        query_name=decision.query_name,
        origin=decision.origin,
        explanation=decision.explanation,
        support=decision.support,
    )


@router.patch("/{author_id}", response_model=AuthorRead)
def update_author_identity(
    author_id: int, payload: AuthorIdentityUpdate, session: SessionDep
) -> AuthorRead:
    repository = AuthorRepository(session)
    author = repository.get(author_id)
    if not author:
        raise HTTPException(status_code=404, detail="作者不存在")
    next_name = payload.name if payload.name is not None else author.name
    next_aliases = payload.aliases if payload.aliases is not None else author.aliases
    next_circles = (
        payload.circle_names if payload.circle_names is not None else author.circle_names
    )
    conflicts = repository.identity_conflicts(
        author.id, [next_name, *next_aliases, *next_circles]
    )
    if conflicts and not payload.confirm_conflicts:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "作者名称或别名与其他身份冲突，需要人工确认",
                "conflicts": conflicts,
            },
        )
    author.name = next_name
    author.aliases = next_aliases
    author.circle_names = next_circles
    if payload.provider_query_names is not None:
        author.provider_query_names = payload.provider_query_names
    if "identity_note" in payload.model_fields_set:
        author.identity_note = payload.identity_note
    session.commit()
    return AuthorRead.model_validate(author)


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

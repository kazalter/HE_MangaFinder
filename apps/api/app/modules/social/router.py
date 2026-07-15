from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import (
    ActivityItem,
    AuthorDigest,
    ReleaseSignal,
    SocialAccount,
    SocialPost,
    WorkGroup,
)
from app.db.session import get_session
from app.modules.authors.repository import AuthorRepository
from app.modules.jobs.repository import JobRepository
from app.modules.jobs.schemas import JobRead
from app.modules.social.collector import XBrowserCollector
from app.modules.social.digest import DigestService
from app.modules.social.repository import SocialRepository
from app.modules.social.schemas import (
    ActivityItemRead,
    AuthorDigestRead,
    ReleaseSignalRead,
    SignalLinkRequest,
    SignalReviewRequest,
    SocialAccountCreate,
    SocialAccountRead,
    SocialAccountSuggestion,
    SocialPostRead,
    SocialStatusRead,
)
from app.modules.social.service import SocialSyncService

router = APIRouter(tags=["social"])
SessionDep = Annotated[Session, Depends(get_session)]


def _require_social_enabled() -> None:
    if not get_settings().social_enabled:
        raise HTTPException(status_code=503, detail="作者动态雷达尚未启用")


def _signal_read(session: Session, signal: ReleaseSignal) -> ReleaseSignalRead:
    repository = SocialRepository(session)
    author = repository.author(signal.author_id)
    posts = repository.signal_posts(signal.id)
    return ReleaseSignalRead(
        id=signal.id,
        author_id=signal.author_id,
        author_name=author.name if author else "已删除作者",
        kind=signal.kind,
        title=signal.title,
        event_code=signal.event_code,
        booth=signal.booth,
        release_date=signal.release_date,
        store_urls=signal.store_urls,
        confidence=signal.confidence,
        status=signal.status,
        is_read=signal.is_read,
        evidence=signal.evidence,
        counter_evidence=signal.counter_evidence,
        missing_information=signal.missing_information,
        linked_group_id=signal.linked_group_id,
        reviewed_by=signal.reviewed_by,
        created_at=signal.created_at,
        updated_at=signal.updated_at,
        posts=[SocialPostRead.model_validate(post) for post in posts],
    )


def _activity_read(session: Session, activity: ActivityItem) -> ActivityItemRead:
    repository = SocialRepository(session)
    author = repository.author(activity.author_id)
    return ActivityItemRead(
        id=activity.id,
        author_id=activity.author_id,
        author_name=author.name if author else "已删除作者",
        category=activity.category,
        headline=activity.headline,
        summary=activity.summary,
        importance=activity.importance,
        confidence=activity.confidence,
        is_read=activity.is_read,
        started_at=activity.started_at,
        ended_at=activity.ended_at,
        posts=[
            SocialPostRead.model_validate(post)
            for post in repository.activity_posts(activity.id)
        ],
    )


def _digest_read(session: Session, digest: AuthorDigest) -> AuthorDigestRead:
    author = SocialRepository(session).author(digest.author_id)
    return AuthorDigestRead(
        id=digest.id,
        author_id=digest.author_id,
        author_name=author.name if author else "已删除作者",
        period_type=digest.period_type,
        period_start=digest.period_start,
        period_end=digest.period_end,
        summary=digest.summary,
        highlights=digest.highlights,
        uncertainties=digest.uncertainties,
        evidence_post_ids=digest.evidence_post_ids,
        generated_by=digest.generated_by,
        model=digest.model,
        error=digest.error,
        created_at=digest.created_at,
        updated_at=digest.updated_at,
    )


@router.get("/social/status", response_model=SocialStatusRead)
def social_status(session: SessionDep) -> SocialStatusRead:
    settings = get_settings()
    repository = SocialRepository(session)
    unread = len(
        list(
            session.scalars(
                select(ReleaseSignal.id).where(ReleaseSignal.is_read.is_(False))
                .where(ReleaseSignal.status.in_(["pending", "confirmed", "linked"]))
            )
        )
    )
    unread += len(
        list(session.scalars(select(ActivityItem.id).where(ActivityItem.is_read.is_(False))))
    )
    return SocialStatusRead(
        enabled=settings.social_enabled,
        collector_configured=bool(
            settings.social_enabled and settings.social_collector_base_url.strip()
        ),
        agent_configured=settings.social_agent_configured,
        qq_configured=settings.qq_bot_configured,
        auto_confirm_threshold=settings.social_auto_confirm_threshold,
        candidate_threshold=settings.social_candidate_threshold,
        pending_count=repository.count_signals("pending"),
        unread_count=unread,
    )


@router.get(
    "/authors/{author_id}/social-accounts", response_model=list[SocialAccountRead]
)
def list_social_accounts(author_id: int, session: SessionDep) -> list[SocialAccountRead]:
    if not AuthorRepository(session).get(author_id):
        raise HTTPException(status_code=404, detail="作者不存在")
    return [
        SocialAccountRead.model_validate(item)
        for item in SocialRepository(session).accounts_for_author(author_id)
    ]


@router.post(
    "/authors/{author_id}/social-accounts",
    response_model=SocialAccountRead,
    status_code=status.HTTP_201_CREATED,
)
def add_social_account(
    author_id: int, payload: SocialAccountCreate, session: SessionDep
) -> SocialAccountRead:
    _require_social_enabled()
    if not AuthorRepository(session).get(author_id):
        raise HTTPException(status_code=404, detail="作者不存在")
    try:
        account = SocialRepository(session).add_account(
            author_id,
            payload.handle,
            payload.account_type,
            payload.confirmed,
            payload.display_name,
        )
        if payload.confirmed:
            JobRepository(session).enqueue_social_sync(account.id)
        session.commit()
        return SocialAccountRead.model_validate(account)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get(
    "/authors/{author_id}/social-account-suggestions",
    response_model=list[SocialAccountSuggestion],
)
async def suggest_social_accounts(
    author_id: int, session: SessionDep
) -> list[SocialAccountSuggestion]:
    author = AuthorRepository(session).get(author_id)
    if not author:
        raise HTTPException(status_code=404, detail="作者不存在")
    settings = get_settings()
    _require_social_enabled()
    try:
        return await XBrowserCollector(settings).suggestions(author.name)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post(
    "/authors/{author_id}/social-accounts/{account_id}/confirm",
    response_model=SocialAccountRead,
)
def confirm_social_account(
    author_id: int, account_id: int, session: SessionDep
) -> SocialAccountRead:
    _require_social_enabled()
    account = SocialRepository(session).get_account(account_id)
    if not account or account.author_id != author_id:
        raise HTTPException(status_code=404, detail="社交账号不存在")
    account.status = "confirmed"
    account.next_sync_at = datetime.now(UTC)
    JobRepository(session).enqueue_social_sync(account.id)
    session.commit()
    return SocialAccountRead.model_validate(account)


@router.delete(
    "/authors/{author_id}/social-accounts/{account_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_social_account(author_id: int, account_id: int, session: SessionDep) -> Response:
    account = SocialRepository(session).get_account(account_id)
    if not account or account.author_id != author_id:
        raise HTTPException(status_code=404, detail="社交账号不存在")
    session.delete(account)
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/authors/{author_id}/social-sync",
    response_model=list[JobRead],
    status_code=status.HTTP_202_ACCEPTED,
)
def sync_author_social(author_id: int, session: SessionDep) -> list[JobRead]:
    _require_social_enabled()
    if not AuthorRepository(session).get(author_id):
        raise HTTPException(status_code=404, detail="作者不存在")
    accounts = [
        account
        for account in SocialRepository(session).accounts_for_author(author_id)
        if account.status == "confirmed"
    ]
    if not accounts:
        raise HTTPException(status_code=409, detail="该作者还没有已确认的 X 账号")
    jobs = [JobRepository(session).enqueue_social_sync(account.id) for account in accounts]
    session.commit()
    return [JobRead.model_validate(job) for job in jobs]


@router.get("/social/radar", response_model=list[ReleaseSignalRead])
def list_radar(
    session: SessionDep,
    author_id: int | None = None,
    signal_status: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[ReleaseSignalRead]:
    rows = SocialRepository(session).list_signals(author_id, signal_status, limit)
    return [_signal_read(session, signal) for signal in rows]


@router.get("/social/activity", response_model=list[ActivityItemRead])
def list_activity(
    session: SessionDep,
    author_id: int | None = None,
    category: str | None = None,
    limit: Annotated[int, Query(ge=1, le=300)] = 100,
) -> list[ActivityItemRead]:
    rows = SocialRepository(session).list_activities(author_id, category, limit)
    return [_activity_read(session, item) for item in rows]


@router.get(
    "/authors/{author_id}/social-digest", response_model=AuthorDigestRead | None
)
def get_author_digest(author_id: int, session: SessionDep) -> AuthorDigestRead | None:
    if not AuthorRepository(session).get(author_id):
        raise HTTPException(status_code=404, detail="作者不存在")
    digest = SocialRepository(session).latest_digest(author_id)
    return _digest_read(session, digest) if digest else None


@router.post(
    "/authors/{author_id}/social-digest/refresh",
    response_model=AuthorDigestRead | None,
)
async def refresh_author_digest(
    author_id: int, session: SessionDep
) -> AuthorDigestRead | None:
    _require_social_enabled()
    if not AuthorRepository(session).get(author_id):
        raise HTTPException(status_code=404, detail="作者不存在")
    digest = await DigestService(session, get_settings()).refresh(author_id)
    session.commit()
    return _digest_read(session, digest) if digest else None


@router.get(
    "/authors/{author_id}/social-posts", response_model=list[SocialPostRead]
)
def list_author_social_posts(
    author_id: int,
    session: SessionDep,
    post_type: str | None = None,
    limit: Annotated[int, Query(ge=1, le=300)] = 100,
) -> list[SocialPostRead]:
    if not AuthorRepository(session).get(author_id):
        raise HTTPException(status_code=404, detail="作者不存在")
    statement = (
        select(SocialPost)
        .join(SocialAccount, SocialAccount.id == SocialPost.account_id)
        .where(SocialAccount.author_id == author_id)
    )
    if post_type:
        statement = statement.where(SocialPost.post_type == post_type)
    rows = list(
        session.scalars(statement.order_by(SocialPost.posted_at.desc()).limit(limit))
    )
    return [SocialPostRead.model_validate(post) for post in rows]


@router.post("/social/activity/{activity_id}/read", status_code=status.HTTP_204_NO_CONTENT)
def mark_activity_read(activity_id: int, session: SessionDep) -> Response:
    item = session.get(ActivityItem, activity_id)
    if not item:
        raise HTTPException(status_code=404, detail="作者动态不存在")
    item.is_read = True
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/social/signals/{signal_id}", response_model=ReleaseSignalRead)
def get_signal(signal_id: int, session: SessionDep) -> ReleaseSignalRead:
    signal = session.get(ReleaseSignal, signal_id)
    if not signal:
        raise HTTPException(status_code=404, detail="动态情报不存在")
    return _signal_read(session, signal)


@router.post("/social/signals/{signal_id}/read", status_code=status.HTTP_204_NO_CONTENT)
def mark_signal_read(signal_id: int, session: SessionDep) -> Response:
    signal = session.get(ReleaseSignal, signal_id)
    if not signal:
        raise HTTPException(status_code=404, detail="动态情报不存在")
    signal.is_read = True
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/social/signals/{signal_id}/review", response_model=ReleaseSignalRead)
def review_signal(
    signal_id: int, payload: SignalReviewRequest, session: SessionDep
) -> ReleaseSignalRead:
    signal = session.get(ReleaseSignal, signal_id)
    if not signal:
        raise HTTPException(status_code=404, detail="动态情报不存在")
    signal.status = "confirmed" if payload.decision == "confirm" else "rejected"
    signal.reviewed_by = "human"
    signal.reviewed_at = datetime.now(UTC)
    signal.is_read = True
    if payload.decision == "confirm":
        post = session.get(SocialPost, signal.primary_post_id)
        account = session.get(SocialAccount, post.account_id) if post else None
        if post and account:
            SocialSyncService(session, get_settings()).queue_notification(signal, account, post)
    session.commit()
    return _signal_read(session, signal)


@router.post("/social/signals/{signal_id}/link-work", response_model=ReleaseSignalRead)
def link_signal(
    signal_id: int, payload: SignalLinkRequest, session: SessionDep
) -> ReleaseSignalRead:
    signal = session.get(ReleaseSignal, signal_id)
    if not signal:
        raise HTTPException(status_code=404, detail="动态情报不存在")
    if not session.get(WorkGroup, payload.group_id):
        raise HTTPException(status_code=404, detail="聚合作品不存在")
    signal.linked_group_id = payload.group_id
    if signal.status == "confirmed":
        signal.status = "linked"
    signal.reviewed_by = "human"
    signal.reviewed_at = datetime.now(UTC)
    signal.is_read = True
    session.commit()
    return _signal_read(session, signal)

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.models import Job
from app.db.session import get_session
from app.modules.agent_review.repository import AgentReviewRepository
from app.modules.agent_review.schemas import (
    AgentReviewRead,
    AgentRunCreate,
    AgentStatusRead,
)
from app.modules.jobs.repository import JobRepository
from app.modules.jobs.schemas import JobRead

router = APIRouter(prefix="/agent-reviews", tags=["agent-reviews"])
SessionDep = Annotated[Session, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def review_read(review: object) -> AgentReviewRead:
    return AgentReviewRead.model_validate(review, from_attributes=True)


@router.get("/status", response_model=AgentStatusRead)
def agent_status(settings: SettingsDep) -> AgentStatusRead:
    return AgentStatusRead(
        enabled=settings.agent_enabled,
        configured=settings.agent_configured,
        provider=settings.agent_provider,
        model=settings.agent_model,
        prompt_version=settings.agent_prompt_version,
        auto_apply=False,
        sends_images=False,
    )


@router.get("", response_model=list[AgentReviewRead])
def list_agent_reviews(
    session: SessionDep, limit: int = Query(default=50, ge=1, le=100)
) -> list[AgentReviewRead]:
    return [review_read(item) for item in AgentReviewRepository(session).list_recent(limit)]


@router.post("/run", response_model=JobRead, status_code=status.HTTP_202_ACCEPTED)
def run_agent_reviews(
    payload: AgentRunCreate, session: SessionDep, settings: SettingsDep
) -> Job:
    if not settings.agent_configured:
        raise HTTPException(
            status_code=409,
            detail="Agent 尚未启用或未配置 MANGAFINDER_AGENT_MODEL",
        )
    job = JobRepository(session).enqueue_agent_reviews(payload.max_reviews)
    session.commit()
    return job

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.models import AdminAccount
from app.db.session import get_session
from app.modules.assistant.repository import AssistantRepository
from app.modules.assistant.schemas import (
    AssistantChatRequest,
    AssistantChatResponse,
    AssistantConfirmResponse,
)
from app.modules.assistant.service import (
    answer_question,
    confirm_proposal,
    enforce_chat_rate_limit,
)
from app.modules.system_config.security import require_admin

router = APIRouter(prefix="/assistant", tags=["assistant"])
SessionDep = Annotated[Session, Depends(get_session)]
AdminDep = Annotated[AdminAccount, Depends(require_admin)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


@router.post("/chat", response_model=AssistantChatResponse)
async def chat(
    payload: AssistantChatRequest,
    session: SessionDep,
    settings: SettingsDep,
    account: AdminDep,
) -> AssistantChatResponse:
    enforce_chat_rate_limit(account.id)
    question = payload.messages[-1].content.strip()
    context = AssistantRepository(session).build_context(question)
    return await answer_question(settings, payload.messages, context, account.id)


@router.post("/proposals/{proposal_id}/confirm", response_model=AssistantConfirmResponse)
def confirm(
    proposal_id: str,
    session: SessionDep,
    account: AdminDep,
) -> AssistantConfirmResponse:
    message, target_type, target_id = confirm_proposal(session, account.id, proposal_id)
    return AssistantConfirmResponse(
        message=message,
        target_type=target_type,
        target_id=target_id,
    )

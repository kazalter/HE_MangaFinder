from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.db.session import get_session
from app.modules.dashboard.repository import DashboardRepository
from app.modules.dashboard.schemas import (
    CatalogEventBulkUpdate,
    CatalogEventRead,
    CatalogEventStateUpdate,
    DashboardRead,
)

router = APIRouter(tags=["dashboard"])
SessionDep = Annotated[Session, Depends(get_session)]


@router.get("/dashboard", response_model=DashboardRead)
def dashboard(
    session: SessionDep,
    days: Annotated[int, Query(ge=7, le=365)] = 30,
) -> DashboardRead:
    return DashboardRepository(session).dashboard(days)


@router.get("/discovery-inbox", response_model=list[CatalogEventRead])
def discovery_inbox(
    session: SessionDep,
    state: Annotated[
        str | None, Query(pattern="^(all|unread|read|interested|ignored)$")
    ] = "unread",
    author_id: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[CatalogEventRead]:
    return DashboardRepository(session).inbox(
        state=state, author_id=author_id, limit=limit
    )


@router.patch("/discovery-inbox/{event_id}", status_code=status.HTTP_204_NO_CONTENT)
def update_discovery_event(
    event_id: int,
    payload: CatalogEventStateUpdate,
    session: SessionDep,
) -> Response:
    if DashboardRepository(session).set_event_state(event_id, payload.state) is None:
        raise HTTPException(status_code=404, detail="发现记录不存在")
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/discovery-inbox/bulk", response_model=dict[str, int])
def update_discovery_events(
    payload: CatalogEventBulkUpdate,
    session: SessionDep,
) -> dict[str, int]:
    changed = DashboardRepository(session).set_all_event_states(
        payload.state, payload.author_id
    )
    session.commit()
    return {"changed": changed}

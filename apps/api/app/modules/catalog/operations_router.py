from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import CatalogOperation
from app.db.session import get_session
from app.modules.catalog.operation_history import UndoConflict, undo_operation

router = APIRouter(prefix="/catalog-operations", tags=["catalog-operations"])
SessionDep = Annotated[Session, Depends(get_session)]


class OperationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    kind: str
    group_ids: list[int]
    before_group_ids: list[int]
    after_group_ids: list[int]
    summary: str
    created_at: datetime
    undone_at: datetime | None


@router.get("", response_model=list[OperationRead])
def list_operations(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    before_id: Annotated[int | None, Query(gt=0)] = None,
):
    if session.get_bind().dialect.name != "sqlite":
        raise HTTPException(status_code=501, detail="当前数据库暂不支持整理记录")
    statement = select(CatalogOperation).order_by(CatalogOperation.id.desc()).limit(limit)
    if before_id is not None:
        statement = statement.where(CatalogOperation.id < before_id)
    return list(session.scalars(statement))


@router.post("/{operation_id}/undo", response_model=OperationRead)
def undo(operation_id: int, session: SessionDep):
    try:
        operation = undo_operation(session, operation_id)
        session.commit()
        return operation
    except LookupError as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except UndoConflict as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc

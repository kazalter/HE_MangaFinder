from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
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
    changes_summary: list[str] = Field(default_factory=list, validation_alias="changes")

    @field_validator("changes_summary", mode="before")
    @classmethod
    def describe_changes(cls, changes: list[dict]) -> list[str]:
        """Expose useful catalog differences without returning raw undo records."""
        descriptions: list[str] = []
        for change in changes or []:
            table = change.get("table")
            before, after = change.get("before"), change.get("after")
            row = after or before or {}
            if table == "work_groups":
                if before is None:
                    descriptions.append(f"新增作品组 {row.get('id')}：《{row.get('title', '')}》")
                elif after is None:
                    descriptions.append(f"移除作品组 {row.get('id')}：《{row.get('title', '')}》")
                elif before.get("title") != after.get("title"):
                    descriptions.append(
                        f"作品组 {row.get('id')} 标题：{before.get('title', '')} → "
                        f"{after.get('title', '')}"
                    )
            elif table == "work_group_members":
                if before is None:
                    descriptions.append(
                        f"版本 {row.get('work_id')} 加入作品组 {row.get('group_id')}"
                    )
                elif after is None:
                    descriptions.append(
                        f"版本 {row.get('work_id')} 移出作品组 {row.get('group_id')}"
                    )
                elif before.get("group_id") != after.get("group_id"):
                    descriptions.append(
                        f"版本 {row.get('work_id')}：作品组 {before.get('group_id')} → "
                        f"{after.get('group_id')}"
                    )
            elif table == "author_works":
                if before is None:
                    descriptions.append(
                        f"作者 {row.get('author_id')} 关联版本 {row.get('work_id')}"
                    )
                elif after is None:
                    descriptions.append(
                        f"作者 {row.get('author_id')} 取消关联版本 {row.get('work_id')}"
                    )
            if len(descriptions) == 100:
                descriptions.append("其余变化已省略")
                break
        return descriptions


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

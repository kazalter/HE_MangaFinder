from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Series, SeriesMember, WorkGroup, WorkRelation
from app.db.session import get_session
from app.modules.catalog.series import SeriesService
from app.modules.catalog.series_schemas import (
    SeriesCandidateGroup,
    SeriesCandidateRead,
    SeriesCreate,
    SeriesMemberInput,
    SeriesMemberRead,
    SeriesRead,
    WorkRelationCreate,
    WorkRelationRead,
)

router = APIRouter(prefix="/series", tags=["series"])
SessionDep = Annotated[Session, Depends(get_session)]


def _series_read(session: Session, series: Series) -> SeriesRead:
    members = list(
        session.scalars(
            select(SeriesMember)
            .where(SeriesMember.series_id == series.id)
            .order_by(SeriesMember.volume_number, SeriesMember.group_id)
        )
    )
    values: list[SeriesMemberRead] = []
    numbers: set[int] = set()
    for member in members:
        group = session.get(WorkGroup, member.group_id)
        if not group:
            continue
        if member.volume_number is not None:
            numbers.add(member.volume_number)
        values.append(
            SeriesMemberRead(
                group_id=group.id,
                title=group.title,
                volume_number=member.volume_number,
                label=member.label,
                confidence=member.confidence,
                is_manual=member.is_manual,
            )
        )
    missing = (
        [number for number in range(min(numbers), max(numbers) + 1) if number not in numbers]
        if numbers
        else []
    )
    return SeriesRead(
        id=series.id,
        title=series.title,
        members=values,
        missing_volumes=missing,
        created_at=series.created_at,
        updated_at=series.updated_at,
    )


def _relation_read(session: Session, relation: WorkRelation) -> WorkRelationRead:
    source = session.get(WorkGroup, relation.source_group_id)
    target = session.get(WorkGroup, relation.target_group_id)
    return WorkRelationRead(
        id=relation.id,
        source_group_id=relation.source_group_id,
        source_title=source.title if source else "已删除作品",
        target_group_id=relation.target_group_id,
        target_title=target.title if target else "已删除作品",
        relation_type=relation.relation_type,
        is_manual=relation.is_manual,
        created_at=relation.created_at,
    )


@router.get("", response_model=list[SeriesRead])
def list_series(session: SessionDep) -> list[SeriesRead]:
    rows = list(session.scalars(select(Series).order_by(Series.title, Series.id)))
    return [_series_read(session, item) for item in rows]


@router.get("/candidates", response_model=list[SeriesCandidateRead])
def list_series_candidates(
    session: SessionDep, author_id: int | None = None
) -> list[SeriesCandidateRead]:
    return [
        SeriesCandidateRead(
            title=item.title,
            normalized_title=item.normalized_title,
            author_ids=list(item.author_ids),
            groups=[
                SeriesCandidateGroup(
                    group_id=group_id, title=title, volume_number=number
                )
                for group_id, title, number in item.groups
            ],
            missing_volumes=list(item.missing_volumes),
        )
        for item in SeriesService(session).candidates(author_id)
    ]


@router.get("/relations/{group_id}", response_model=list[WorkRelationRead])
def list_work_relations(group_id: int, session: SessionDep) -> list[WorkRelationRead]:
    if not session.get(WorkGroup, group_id):
        raise HTTPException(status_code=404, detail="作品不存在")
    return [
        _relation_read(session, item)
        for item in SeriesService(session).relations_for_group(group_id)
    ]


@router.post("", response_model=SeriesRead, status_code=status.HTTP_201_CREATED)
def create_series(payload: SeriesCreate, session: SessionDep) -> SeriesRead:
    service = SeriesService(session)
    try:
        series = service.create(payload.title, [], manual=not payload.candidate)
        for member in payload.members:
            service.set_member(
                series.id,
                member.group_id,
                member.volume_number,
                label=member.label,
                manual=not payload.candidate,
            )
        session.commit()
        return _series_read(session, series)
    except LookupError as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/{series_id}", response_model=SeriesRead)
def get_series(series_id: int, session: SessionDep) -> SeriesRead:
    series = session.get(Series, series_id)
    if not series:
        raise HTTPException(status_code=404, detail="系列不存在")
    return _series_read(session, series)


@router.put("/{series_id}/members/{group_id}", response_model=SeriesRead)
def set_series_member(
    series_id: int, group_id: int, payload: SeriesMemberInput, session: SessionDep
) -> SeriesRead:
    if payload.group_id != group_id:
        raise HTTPException(status_code=422, detail="作品 ID 不一致")
    try:
        SeriesService(session).set_member(
            series_id,
            group_id,
            payload.volume_number,
            label=payload.label,
            manual=True,
        )
        session.commit()
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    series = session.get(Series, series_id)
    if not series:
        raise HTTPException(status_code=404, detail="系列不存在")
    return _series_read(session, series)


@router.delete(
    "/{series_id}/members/{group_id}", status_code=status.HTTP_204_NO_CONTENT
)
def remove_series_member(series_id: int, group_id: int, session: SessionDep) -> Response:
    if not SeriesService(session).remove_member(series_id, group_id):
        raise HTTPException(status_code=404, detail="系列成员不存在")
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/relations", response_model=WorkRelationRead, status_code=status.HTTP_201_CREATED
)
def create_work_relation(
    payload: WorkRelationCreate, session: SessionDep
) -> WorkRelationRead:
    try:
        relation = SeriesService(session).add_relation(
            payload.source_group_id, payload.target_group_id, payload.relation_type
        )
        session.commit()
        return _relation_read(session, relation)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/relations/{relation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_work_relation(relation_id: int, session: SessionDep) -> Response:
    relation = session.get(WorkRelation, relation_id)
    if not relation:
        raise HTTPException(status_code=404, detail="作品关系不存在")
    session.delete(relation)
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)

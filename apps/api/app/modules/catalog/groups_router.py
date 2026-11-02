from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, object_session

from app.core.time import utc_timestamp
from app.db.models import (
    Author,
    AuthorWork,
    MergeSuggestion,
    Series,
    SeriesMember,
    WorkGroup,
    WorkRelation,
)
from app.db.session import get_session
from app.modules.agent_review.candidates import build_candidate_evidence, evidence_hash
from app.modules.agent_review.repository import AgentReviewRepository
from app.modules.agent_review.router import review_read
from app.modules.agent_review.schemas import CandidateEvidence
from app.modules.catalog.aggregation import AggregationService
from app.modules.catalog.exclusions import WorkExclusionService
from app.modules.catalog.group_repository import WorkGroupRepository
from app.modules.catalog.operation_history import lock_catalog_write, record_operation
from app.modules.catalog.pair_identity import candidate_key
from app.modules.catalog.schemas import (
    EditionRead,
    GroupMergeCreate,
    MergeSuggestionRead,
    MetadataSourceRead,
    MetadataUpdate,
    WorkGroupDetail,
    WorkGroupRead,
    WorkRelationSummary,
    WorkSeriesRead,
    WorkSourceRead,
)
from app.modules.media.urls import group_cover_url, work_cover_url

router = APIRouter(prefix="/work-groups", tags=["work-groups"])
SessionDep = Annotated[Session, Depends(get_session)]


def _edition(member: object) -> EditionRead:
    work = member.work
    dates = [source.source_updated_at for source in work.sources if source.source_updated_at]
    return EditionRead(
        work_id=work.id,
        title=work.title,
        description=work.description,
        cover_url=work_cover_url(work),
        status=work.status,
        year=work.year,
        language=work.language,
        tags=work.tags or [],
        variant_labels=work.fingerprint.variant_labels if work.fingerprint else [],
        page_count=work.fingerprint.page_count if work.fingerprint else None,
        latest_source_at=max(dates, default=None),
        confidence=member.confidence,
        match_method=member.match_method,
        is_manual=member.is_manual,
        authors=[
            {"id": author.id, "name": author.name}
            for author in sorted(work.authors, key=lambda item: item.name.casefold())
        ],
        sources=[
            WorkSourceRead(
                provider=source.provider,
                external_id=source.external_id,
                source_url=source.source_url,
                source_updated_at=source.source_updated_at,
            )
            for source in work.sources
        ],
    )


def _summary(group: WorkGroup) -> WorkGroupRead:
    providers = sorted(
        {source.provider for member in group.members for source in member.work.sources}
    )
    authors = sorted(
        {(author.id, author.name) for member in group.members for author in member.work.authors},
        key=lambda item: item[1].casefold(),
    )
    return WorkGroupRead(
        id=group.id,
        title=group.title,
        description=group.description,
        cover_url=group_cover_url(group),
        status=group.status,
        year=group.year,
        language=group.language,
        tags=group.tags or [],
        first_source_at=group.first_source_at,
        latest_source_at=group.latest_source_at,
        edition_count=len(group.members),
        providers=providers,
        authors=[{"id": author_id, "name": name} for author_id, name in authors],
    )


def _metadata_sources(group: WorkGroup) -> dict[str, MetadataSourceRead]:
    overrides = group.manual_metadata or {}
    works = [member.work for member in group.members]
    result = {}
    for field in ("title", "description", "cover_url", "status", "year", "language", "tags"):
        override_key = "cover_work_id" if field == "cover_url" else field
        locked = override_key in overrides
        matching = []
        if field in overrides and field != "cover_url":
            matching = []  # a manually typed value has no source edition
        elif field == "tags":
            matching = [work for work in works if work.tags]
        else:
            value = getattr(group, field)
            if value is not None:
                for work in works:
                    candidates = [getattr(work, field)]
                    if field == "title" and work.fingerprint:
                        candidates.append(work.fingerprint.normalized_title)
                    if value in candidates:
                        matching.append(work)
        result[field] = MetadataSourceRead(
            locked=locked,
            edition_ids=sorted(work.id for work in matching),
            providers=sorted({source.provider for work in matching for source in work.sources}),
        )
    return result


def _detail(group: WorkGroup) -> WorkGroupDetail:
    summary = _summary(group)
    editions = [_edition(member) for member in group.members]
    editions.sort(key=lambda item: item.title.casefold())
    editions.sort(
        key=lambda item: utc_timestamp(item.latest_source_at) if item.latest_source_at else 0.0,
        reverse=True,
    )
    preferred_id = (group.manual_metadata or {}).get("preferred_work_id")
    editions.sort(key=lambda item: item.work_id != preferred_id)
    session = object_session(group)
    series_read = None
    relations: list[WorkRelationSummary] = []
    if session:
        membership = session.scalar(select(SeriesMember).where(SeriesMember.group_id == group.id))
        if membership:
            series = session.get(Series, membership.series_id)
            numbers = set(
                session.scalars(
                    select(SeriesMember.volume_number).where(
                        SeriesMember.series_id == membership.series_id,
                        SeriesMember.volume_number.is_not(None),
                    )
                )
            )
            missing = (
                [
                    number
                    for number in range(min(numbers), max(numbers) + 1)
                    if number not in numbers
                ]
                if numbers
                else []
            )
            if series:
                series_read = WorkSeriesRead(
                    id=series.id,
                    title=series.title,
                    volume_number=membership.volume_number,
                    label=membership.label,
                    missing_volumes=missing,
                )
        relation_rows = session.scalars(
            select(WorkRelation).where(
                or_(
                    WorkRelation.source_group_id == group.id,
                    WorkRelation.target_group_id == group.id,
                )
            )
        )
        for relation in relation_rows:
            outgoing = relation.source_group_id == group.id
            related_id = relation.target_group_id if outgoing else relation.source_group_id
            related = session.get(WorkGroup, related_id)
            relations.append(
                WorkRelationSummary(
                    id=relation.id,
                    related_group_id=related_id,
                    related_title=related.title if related else "已删除作品",
                    relation_type=relation.relation_type,
                    direction="outgoing" if outgoing else "incoming",
                )
            )
    return WorkGroupDetail(
        **summary.model_dump(),
        editions=editions,
        metadata_overrides=group.manual_metadata or {},
        metadata_sources=_metadata_sources(group),
        series=series_read,
        relations=relations,
    )


def _suggestion(item: MergeSuggestion, repository: WorkGroupRepository) -> MergeSuggestionRead:
    source = repository.get(item.source_group_id)
    target = repository.get(item.target_group_id)
    evidence = (
        build_candidate_evidence(item, source, target)
        if source is not None and target is not None
        else None
    )
    latest_review = AgentReviewRepository(repository.session).latest_for_suggestion(item.id)
    review_is_stale = bool(
        latest_review and evidence and latest_review.evidence_hash != evidence_hash(evidence)
    )
    return MergeSuggestionRead(
        id=item.id,
        source_group_id=item.source_group_id,
        source_title=source.title if source else "已删除作品",
        target_group_id=item.target_group_id,
        target_title=target.title if target else "已删除作品",
        source_group=_summary(source) if source else None,
        target_group=_summary(target) if target else None,
        confidence=item.confidence,
        reasons=item.reasons or [],
        status=item.status,
        agent_review=(
            review_read(latest_review, is_stale=review_is_stale) if latest_review else None
        ),
        hard_conflicts=list(evidence.hard_conflicts) if evidence else [],
        soft_conflicts=list(evidence.soft_conflicts) if evidence else [],
        conflict_details=_conflict_details(evidence) if evidence else [],
        core_title_similarity=evidence.core_title_similarity if evidence else None,
        cover_hash_distance=evidence.cover_hash_distance if evidence else None,
        cover_match_mode=evidence.cover_match_mode if evidence else None,
        cover_legacy_distance=evidence.cover_legacy_distance if evidence else None,
        source_identity_titles=(
            sorted({edition.identity_core for edition in evidence.left.editions})
            if evidence
            else []
        ),
        target_identity_titles=(
            sorted({edition.identity_core for edition in evidence.right.editions})
            if evidence
            else []
        ),
        shared_context=list(evidence.shared_context) if evidence else [],
    )


def _conflict_details(evidence: CandidateEvidence) -> list[str]:
    details: list[str] = []
    conflicts = {*evidence.hard_conflicts, *evidence.soft_conflicts}
    if "year_mismatch" in conflicts:
        left = sorted({item.year for item in evidence.left.editions if item.year is not None})
        right = sorted({item.year for item in evidence.right.editions if item.year is not None})
        details.append(f"年份：{left} vs {right}（可能是来源上传年份）")
    if "page_count_mismatch" in conflicts:
        left = sorted(
            {item.page_count for item in evidence.left.editions if item.page_count is not None}
        )
        right = sorted(
            {item.page_count for item in evidence.right.editions if item.page_count is not None}
        )
        details.append(f"页数：{left} vs {right}（版本附页可能造成差异）")
    if "number_mismatch" in conflicts:
        left = sorted({tuple(item.number_signature) for item in evidence.left.editions})
        right = sorted({tuple(item.number_signature) for item in evidence.right.editions})
        details.append(f"作品编号：{left} vs {right}")
    if "author_mismatch" in conflicts:
        left = sorted({name for item in evidence.left.editions for name in item.authors})
        right = sorted({name for item in evidence.right.editions for name in item.authors})
        details.append(f"作者：{left} vs {right}")
    if "core_title_mismatch" in conflicts:
        left = sorted({item.identity_core for item in evidence.left.editions})
        right = sorted({item.identity_core for item in evidence.right.editions})
        details.append(f"核心标题：{left} vs {right}")
    if "cover_dissimilar" in conflicts and evidence.cover_hash_distance is not None:
        details.append(f"封面明显不同：裁切感知视觉距离 {evidence.cover_hash_distance}")
    if "insufficient_evidence" in conflicts:
        details.append("缺少两个相互独立的作品身份信号")
    return details


@router.get("", response_model=list[WorkGroupRead])
def list_work_groups(session: SessionDep, author_id: int | None = None) -> list[WorkGroupRead]:
    return [_summary(group) for group in WorkGroupRepository(session).list_groups(author_id)]


@router.get("/suggestions", response_model=list[MergeSuggestionRead])
def list_suggestions(
    session: SessionDep,
    status: str = Query(default="pending", pattern="^(pending|accepted|rejected)$"),
) -> list[MergeSuggestionRead]:
    repository = WorkGroupRepository(session)
    return [_suggestion(item, repository) for item in repository.suggestions(status)]


@router.post("/suggestions/{suggestion_id}/accept", response_model=WorkGroupDetail)
def accept_suggestion(suggestion_id: int, session: SessionDep) -> WorkGroupDetail:
    lock_catalog_write(session)
    repository = WorkGroupRepository(session)
    item = repository.suggestion(suggestion_id)
    if item is None or item.status != "pending":
        raise HTTPException(status_code=404, detail="待处理聚合建议不存在")
    source = repository.get(item.source_group_id)
    target = repository.get(item.target_group_id)
    if source is None or target is None:
        raise HTTPException(status_code=409, detail="候选作品已发生变化")
    if source.id == target.id:
        raise HTTPException(status_code=409, detail="不能将作品与自身合并")
    try:
        with record_operation(
            session, "merge", [target.id, source.id], f"合并《{source.title}》到《{target.title}》"
        ) as journal:
            merged = AggregationService(session).merge_groups(target, source)
            if journal:
                journal.operation.after_group_ids = [merged.id]
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    session.commit()
    return _detail(merged)


@router.post("/suggestions/{suggestion_id}/reject", response_model=MergeSuggestionRead)
def reject_suggestion(suggestion_id: int, session: SessionDep) -> MergeSuggestionRead:
    lock_catalog_write(session)
    repository = WorkGroupRepository(session)
    item = repository.suggestion(suggestion_id)
    if item is None or item.status != "pending":
        raise HTTPException(status_code=404, detail="待处理聚合建议不存在")
    source = repository.get(item.source_group_id)
    target = repository.get(item.target_group_id)
    if source is not None and target is not None:
        review_repository = AgentReviewRepository(session)
        latest = review_repository.latest_for_suggestion(item.id)
        review_repository.constrain_pair(
            candidate_key(source, target),
            origin="human_rejection",
            review_id=latest.id if latest else None,
            note="用户在聚合候选审核中选择保持分开",
        )
    item.status = "rejected"
    session.commit()
    return _suggestion(item, repository)


@router.get("/{group_id}", response_model=WorkGroupDetail)
def get_work_group(group_id: int, session: SessionDep) -> WorkGroupDetail:
    group = WorkGroupRepository(session).get(group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="聚合作品不存在")
    return _detail(group)


@router.patch("/{group_id}/metadata", response_model=WorkGroupDetail)
def update_metadata(group_id: int, payload: MetadataUpdate, session: SessionDep) -> WorkGroupDetail:
    lock_catalog_write(session)
    group = WorkGroupRepository(session).get(group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="聚合作品不存在")
    overrides = dict(group.manual_metadata or {})
    values = payload.model_dump(exclude_unset=True)
    works = {member.work_id: member.work for member in group.members}
    for key, value in values.items():
        if value is None:
            overrides.pop(key, None)
            continue
        if key in {"cover_work_id", "preferred_work_id"}:
            if value not in works:
                raise HTTPException(status_code=422, detail="所选版本不属于该作品")
            if key == "cover_work_id" and not works[value].cover_url:
                raise HTTPException(status_code=422, detail="所选版本没有封面")
        if key in {"title", "language"}:
            value = value.strip()
            if not value:
                raise HTTPException(status_code=422, detail="标题和语言不能为空")
        if key == "tags":
            if any(not tag.strip() or len(tag) > 100 for tag in value):
                raise HTTPException(status_code=422, detail="标签不能为空或超过 100 字")
            value = list(dict.fromkeys(tag.strip() for tag in value))
        overrides[key] = value
    group.manual_metadata = overrides
    AggregationService(session).recompute(group)
    session.commit()
    return _detail(group)


@router.post("/{target_group_id}/merge", response_model=WorkGroupDetail)
def merge_work_groups(
    target_group_id: int, payload: GroupMergeCreate, session: SessionDep
) -> WorkGroupDetail:
    lock_catalog_write(session)
    repository = WorkGroupRepository(session)
    target = repository.get(target_group_id)
    source = repository.get(payload.source_group_id)
    if target is None or source is None:
        raise HTTPException(status_code=404, detail="待合并作品不存在")
    if source.id == target.id:
        raise HTTPException(status_code=409, detail="不能将作品与自身合并")
    try:
        with record_operation(
            session, "merge", [target.id, source.id], f"合并《{source.title}》到《{target.title}》"
        ) as journal:
            merged = AggregationService(session).merge_groups(target, source)
            if journal:
                journal.operation.after_group_ids = [merged.id]
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    session.commit()
    return _detail(merged)


@router.delete("/{group_id}/authors/{author_id}", status_code=status.HTTP_204_NO_CONTENT)
def exclude_work_group_for_author(group_id: int, author_id: int, session: SessionDep) -> Response:
    lock_catalog_write(session)
    repository = WorkGroupRepository(session)
    group = repository.get(group_id)
    author = session.get(Author, author_id)
    if group is None or author is None:
        raise HTTPException(status_code=404, detail="作品或作者不存在")
    works = [
        member.work
        for member in group.members
        if session.get(AuthorWork, (author_id, member.work_id)) is not None
    ]
    if not works:
        raise HTTPException(status_code=404, detail="该作品不属于所选作者")
    try:
        with record_operation(
            session, "exclude", [group_id], f"从 {author.name} 名下排除《{group.title}》"
        ) as journal:
            service = WorkExclusionService(session)
            for work in works:
                service.exclude_work(author, work)
            if journal:
                journal.operation.after_group_ids = (
                    [group_id] if session.get(WorkGroup, group_id) else []
                )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/{group_id}/members/{work_id}/authors/{author_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def exclude_work_group_member_for_author(
    group_id: int, work_id: int, author_id: int, session: SessionDep
) -> Response:
    lock_catalog_write(session)
    group = WorkGroupRepository(session).get(group_id)
    author = session.get(Author, author_id)
    if group is None or author is None:
        raise HTTPException(status_code=404, detail="作品或作者不存在")
    member = next((item for item in group.members if item.work_id == work_id), None)
    if member is None:
        raise HTTPException(status_code=404, detail="版本不属于该作品")
    try:
        with record_operation(
            session, "exclude", [group_id], f"从 {author.name} 名下排除版本《{member.work.title}》"
        ) as journal:
            WorkExclusionService(session).exclude_work(author, member.work)
            if journal:
                journal.operation.after_group_ids = (
                    [group_id] if session.get(WorkGroup, group_id) else []
                )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{group_id}/members/{work_id}/split", response_model=WorkGroupDetail)
def split_work_group_member(group_id: int, work_id: int, session: SessionDep) -> WorkGroupDetail:
    lock_catalog_write(session)
    repository = WorkGroupRepository(session)
    group = repository.get(group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="聚合作品不存在")
    member = next((item for item in group.members if item.work_id == work_id), None)
    if member is None:
        raise HTTPException(status_code=404, detail="版本不属于该作品")
    try:
        with record_operation(
            session, "split", [group_id], f"从《{group.title}》拆出版本《{member.work.title}》"
        ) as journal:
            new_group = AggregationService(session).split_member(group, work_id)
            if journal:
                journal.operation.group_ids = [group_id, new_group.id]
                journal.operation.after_group_ids = [group_id, new_group.id]
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    session.commit()
    return _detail(new_group)

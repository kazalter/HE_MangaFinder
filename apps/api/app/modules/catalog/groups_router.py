from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db.models import MergeSuggestion, WorkGroup
from app.db.session import get_session
from app.modules.agent_review.candidates import build_candidate_evidence, evidence_hash
from app.modules.agent_review.repository import AgentReviewRepository
from app.modules.agent_review.router import review_read
from app.modules.agent_review.schemas import CandidateEvidence
from app.modules.catalog.aggregation import AggregationService
from app.modules.catalog.group_repository import WorkGroupRepository
from app.modules.catalog.pair_identity import candidate_key
from app.modules.catalog.schemas import (
    EditionRead,
    GroupMergeCreate,
    MergeSuggestionRead,
    WorkGroupDetail,
    WorkGroupRead,
    WorkSourceRead,
)

router = APIRouter(prefix="/work-groups", tags=["work-groups"])
SessionDep = Annotated[Session, Depends(get_session)]


def _edition(member: object) -> EditionRead:
    work = member.work
    dates = [source.source_updated_at for source in work.sources if source.source_updated_at]
    return EditionRead(
        work_id=work.id,
        title=work.title,
        description=work.description,
        cover_url=work.cover_url,
        status=work.status,
        year=work.year,
        language=work.language,
        tags=work.tags or [],
        variant_labels=work.fingerprint.variant_labels if work.fingerprint else [],
        latest_source_at=max(dates, default=None),
        confidence=member.confidence,
        match_method=member.match_method,
        is_manual=member.is_manual,
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
        {
            source.provider
            for member in group.members
            for source in member.work.sources
        }
    )
    return WorkGroupRead(
        id=group.id,
        title=group.title,
        description=group.description,
        cover_url=group.cover_url,
        status=group.status,
        year=group.year,
        language=group.language,
        tags=group.tags or [],
        latest_source_at=group.latest_source_at,
        edition_count=len(group.members),
        providers=providers,
    )


def _detail(group: WorkGroup) -> WorkGroupDetail:
    summary = _summary(group)
    editions = [_edition(member) for member in group.members]
    editions.sort(key=lambda item: item.title.casefold())
    editions.sort(
        key=lambda item: item.latest_source_at.timestamp()
        if item.latest_source_at
        else 0.0,
        reverse=True,
    )
    return WorkGroupDetail(**summary.model_dump(), editions=editions)


def _suggestion(
    item: MergeSuggestion, repository: WorkGroupRepository
) -> MergeSuggestionRead:
    source = repository.get(item.source_group_id)
    target = repository.get(item.target_group_id)
    evidence = (
        build_candidate_evidence(item, source, target)
        if source is not None and target is not None
        else None
    )
    latest_review = AgentReviewRepository(repository.session).latest_for_suggestion(item.id)
    review_is_stale = bool(
        latest_review
        and evidence
        and latest_review.evidence_hash != evidence_hash(evidence)
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
            review_read(latest_review, is_stale=review_is_stale)
            if latest_review
            else None
        ),
        hard_conflicts=list(evidence.hard_conflicts) if evidence else [],
        soft_conflicts=list(evidence.soft_conflicts) if evidence else [],
        conflict_details=_conflict_details(evidence) if evidence else [],
        core_title_similarity=evidence.core_title_similarity if evidence else None,
        cover_hash_distance=evidence.cover_hash_distance if evidence else None,
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
        details.append(f"封面明显不同：感知哈希距离 {evidence.cover_hash_distance}")
    if "insufficient_evidence" in conflicts:
        details.append("缺少两个相互独立的作品身份信号")
    return details


@router.get("", response_model=list[WorkGroupRead])
def list_work_groups(
    session: SessionDep, author_id: int | None = None
) -> list[WorkGroupRead]:
    return [
        _summary(group) for group in WorkGroupRepository(session).list_groups(author_id)
    ]


@router.get("/suggestions", response_model=list[MergeSuggestionRead])
def list_suggestions(
    session: SessionDep,
    status: str = Query(default="pending", pattern="^(pending|accepted|rejected)$"),
) -> list[MergeSuggestionRead]:
    repository = WorkGroupRepository(session)
    return [_suggestion(item, repository) for item in repository.suggestions(status)]


@router.post("/suggestions/{suggestion_id}/accept", response_model=WorkGroupDetail)
def accept_suggestion(suggestion_id: int, session: SessionDep) -> WorkGroupDetail:
    repository = WorkGroupRepository(session)
    item = repository.suggestion(suggestion_id)
    if item is None or item.status != "pending":
        raise HTTPException(status_code=404, detail="待处理聚合建议不存在")
    source = repository.get(item.source_group_id)
    target = repository.get(item.target_group_id)
    if source is None or target is None:
        raise HTTPException(status_code=409, detail="候选作品已发生变化")
    merged = AggregationService(session).merge_groups(target, source)
    session.commit()
    return _detail(merged)


@router.post("/suggestions/{suggestion_id}/reject", response_model=MergeSuggestionRead)
def reject_suggestion(suggestion_id: int, session: SessionDep) -> MergeSuggestionRead:
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


@router.post("/{target_group_id}/merge", response_model=WorkGroupDetail)
def merge_work_groups(
    target_group_id: int, payload: GroupMergeCreate, session: SessionDep
) -> WorkGroupDetail:
    repository = WorkGroupRepository(session)
    target = repository.get(target_group_id)
    source = repository.get(payload.source_group_id)
    if target is None or source is None:
        raise HTTPException(status_code=404, detail="待合并作品不存在")
    merged = AggregationService(session).merge_groups(target, source)
    session.commit()
    return _detail(merged)


@router.post("/{group_id}/members/{work_id}/split", response_model=WorkGroupDetail)
def split_work_group_member(
    group_id: int, work_id: int, session: SessionDep
) -> WorkGroupDetail:
    repository = WorkGroupRepository(session)
    group = repository.get(group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="聚合作品不存在")
    try:
        new_group = AggregationService(session).split_member(group, work_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    session.commit()
    return _detail(new_group)

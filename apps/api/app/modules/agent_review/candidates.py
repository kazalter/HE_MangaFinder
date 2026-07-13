import hashlib
import json
from difflib import SequenceMatcher

from app.db.models import MergeSuggestion, WorkGroup
from app.modules.agent_review.schemas import (
    CandidateEvidence,
    ConflictCode,
    EditionEvidence,
    EvidenceCode,
    GroupEvidence,
    SourceEvidence,
)
from app.modules.catalog.aggregation import cover_hash_distance, identity_number_signature
from app.modules.catalog.pair_identity import candidate_key


def evidence_hash(evidence: CandidateEvidence) -> str:
    payload = evidence.model_dump(mode="json", exclude={"suggestion_id"})
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def build_candidate_evidence(
    suggestion: MergeSuggestion, left: WorkGroup, right: WorkGroup
) -> CandidateEvidence:
    left_evidence = _group_evidence(left)
    right_evidence = _group_evidence(right)
    title_score = _title_similarity(left_evidence, right_evidence)
    cover_distance = _cover_distance(left_evidence, right_evidence)
    available, hard_conflicts, soft_conflicts = _signals(
        left_evidence, right_evidence, title_score, cover_distance
    )
    return CandidateEvidence(
        candidate_key=candidate_key(left, right),
        suggestion_id=suggestion.id,
        rule_confidence=suggestion.confidence,
        rule_reasons=suggestion.reasons or [],
        title_similarity=round(title_score, 5),
        cover_hash_distance=cover_distance,
        available_evidence=available,
        hard_conflicts=hard_conflicts,
        soft_conflicts=soft_conflicts,
        left=left_evidence,
        right=right_evidence,
    )


def _group_evidence(group: WorkGroup) -> GroupEvidence:
    editions: list[EditionEvidence] = []
    for member in group.members:
        work = member.work
        fingerprint = work.fingerprint
        normalized = fingerprint.normalized_title if fingerprint else work.title.casefold()
        editions.append(
            EditionEvidence(
                work_id=work.id,
                raw_title=work.title,
                normalized_title=normalized,
                aliases=fingerprint.title_aliases if fingerprint else [],
                number_signature=list(identity_number_signature(normalized)),
                variant_labels=fingerprint.variant_labels if fingerprint else [],
                language=work.language,
                year=work.year,
                page_count=fingerprint.page_count if fingerprint else None,
                cover_hash=fingerprint.cover_hash if fingerprint else None,
                tags=work.tags or [],
                authors=sorted(author.name for author in work.authors),
                sources=[
                    SourceEvidence(provider=source.provider, external_id=source.external_id)
                    for source in sorted(
                        work.sources, key=lambda item: (item.provider, item.external_id)
                    )
                ],
            )
        )
    return GroupEvidence(
        group_id=group.id,
        display_title=group.title,
        has_manual_members=any(member.is_manual for member in group.members),
        editions=editions,
    )


def _names(group: GroupEvidence) -> set[str]:
    return {
        name
        for edition in group.editions
        for name in [edition.normalized_title, *edition.aliases]
        if name
    }


def _title_similarity(left: GroupEvidence, right: GroupEvidence) -> float:
    return max(
        (
            SequenceMatcher(None, left_name, right_name).ratio()
            for left_name in _names(left)
            for right_name in _names(right)
        ),
        default=0.0,
    )


def _cover_distance(left: GroupEvidence, right: GroupEvidence) -> int | None:
    distances = [
        distance
        for left_edition in left.editions
        for right_edition in right.editions
        if (
            distance := cover_hash_distance(
                left_edition.cover_hash, right_edition.cover_hash
            )
        )
        is not None
    ]
    return min(distances, default=None)


def _signals(
    left: GroupEvidence,
    right: GroupEvidence,
    title_score: float,
    cover_distance: int | None,
) -> tuple[list[EvidenceCode], list[ConflictCode], list[ConflictCode]]:
    evidence: list[EvidenceCode] = []
    hard_conflicts: list[ConflictCode] = []
    soft_conflicts: list[ConflictCode] = []
    left_names, right_names = _names(left), _names(right)
    if left_names & right_names:
        evidence.append("normalized_title_match")
    elif title_score >= 0.68:
        evidence.append("title_similarity")

    left_numbers = {tuple(item.number_signature) for item in left.editions}
    right_numbers = {tuple(item.number_signature) for item in right.editions}
    if left_numbers == right_numbers:
        evidence.append("number_match")
    else:
        hard_conflicts.append("number_mismatch")

    if cover_distance is not None and cover_distance <= 10:
        evidence.append("cover_hash_match")
    left_pages = {item.page_count for item in left.editions if item.page_count is not None}
    right_pages = {item.page_count for item in right.editions if item.page_count is not None}
    if left_pages and right_pages:
        minimum_delta = min(abs(a - b) for a in left_pages for b in right_pages)
        maximum_pages = max(*left_pages, *right_pages)
        if minimum_delta <= max(2, round(maximum_pages * 0.03)):
            evidence.append("page_count_match")
        elif minimum_delta > max(5, round(maximum_pages * 0.15)):
            soft_conflicts.append("page_count_mismatch")

    left_years = {item.year for item in left.editions if item.year is not None}
    right_years = {item.year for item in right.editions if item.year is not None}
    if left_years and right_years:
        minimum_year_delta = min(abs(a - b) for a in left_years for b in right_years)
        if minimum_year_delta <= 1:
            evidence.append("year_match")
        else:
            soft_conflicts.append("year_mismatch")

    left_authors = {name.casefold() for item in left.editions for name in item.authors}
    right_authors = {name.casefold() for item in right.editions for name in item.authors}
    if left_authors and right_authors:
        if left_authors & right_authors:
            evidence.append("author_match")
        else:
            hard_conflicts.append("author_mismatch")

    left_providers = {source.provider for item in left.editions for source in item.sources}
    right_providers = {source.provider for item in right.editions for source in item.sources}
    if left_providers & right_providers:
        evidence.append("provider_overlap")
    left_variants = {label for item in left.editions for label in item.variant_labels}
    right_variants = {label for item in right.editions for label in item.variant_labels}
    if left_variants != right_variants and (left_variants or right_variants):
        evidence.append("variant_difference")
    left_languages = {item.language for item in left.editions if item.language}
    right_languages = {item.language for item in right.editions if item.language}
    if left_languages != right_languages and (left_languages or right_languages):
        evidence.append("language_difference")
    return evidence, hard_conflicts, soft_conflicts

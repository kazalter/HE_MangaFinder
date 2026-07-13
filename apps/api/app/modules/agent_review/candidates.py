import hashlib
import json

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
from app.modules.catalog.title_identity import (
    best_identity_similarity,
    parse_title_identity,
)

_SOURCE_METADATA_KEYS = {
    "altTitles",
    "artists",
    "group",
    "groups",
    "circle",
    "parodies",
    "series",
    "characters",
    "scanlator",
    "category",
    "page_count",
    "uploaded_at",
}


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
    core_score = _title_similarity(left_evidence, right_evidence)
    cover_distances = _cover_distances(left_evidence, right_evidence)
    cover_distance = min(cover_distances, default=None)
    page_delta, page_ratio = _page_comparison(left_evidence, right_evidence)
    shared_terms = sorted(_contexts(left_evidence) & _contexts(right_evidence))
    available, hard_conflicts, soft_conflicts, context_only = _signals(
        left_evidence,
        right_evidence,
        core_score,
        cover_distances,
        page_delta,
        page_ratio,
        shared_terms,
    )
    return CandidateEvidence(
        candidate_key=candidate_key(left, right),
        suggestion_id=suggestion.id,
        rule_confidence=suggestion.confidence,
        rule_reasons=suggestion.reasons or [],
        title_similarity=round(core_score, 5),
        core_title_similarity=round(core_score, 5),
        cover_hash_distance=cover_distance,
        cover_hash_distances=cover_distances,
        page_count_delta=page_delta,
        page_count_ratio=page_ratio,
        shared_context=shared_terms,
        context_only=context_only,
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
        title = parse_title_identity(work.title, normalized)
        editions.append(
            EditionEvidence(
                work_id=work.id,
                raw_title=work.title,
                normalized_title=normalized,
                identity_core=title.identity_core,
                context_terms=list(title.context_terms),
                aliases=fingerprint.title_aliases if fingerprint else [],
                number_signature=list(identity_number_signature(title.identity_core)),
                variant_labels=fingerprint.variant_labels if fingerprint else [],
                language=work.language,
                year=work.year,
                page_count=fingerprint.page_count if fingerprint else None,
                cover_hash=fingerprint.cover_hash if fingerprint else None,
                tags=work.tags or [],
                authors=sorted(author.name for author in work.authors),
                sources=[
                    SourceEvidence(
                        provider=source.provider,
                        external_id=source.external_id,
                        source_updated_at=(
                            source.source_updated_at.isoformat()
                            if source.source_updated_at
                            else None
                        ),
                        metadata=_source_metadata(source.raw_metadata or {}),
                    )
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


def _source_metadata(raw: dict[str, object]) -> dict[str, object]:
    return {
        key: _bounded(value)
        for key, value in raw.items()
        if key in _SOURCE_METADATA_KEYS and value not in (None, "", [], {})
    }


def _bounded(value: object) -> object:
    if isinstance(value, str):
        return value[:500]
    if isinstance(value, list):
        return [_bounded(item) for item in value[:20]]
    if isinstance(value, dict):
        return {str(key)[:80]: _bounded(item) for key, item in list(value.items())[:20]}
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:500]


def _core_names(group: GroupEvidence) -> set[str]:
    return {item.identity_core for item in group.editions if item.identity_core}


def _alias_names(group: GroupEvidence) -> set[str]:
    return {
        parse_title_identity(alias, alias).identity_core
        for edition in group.editions
        for alias in edition.aliases
        if alias
    }


def _all_identity_names(group: GroupEvidence) -> set[str]:
    return _core_names(group) | _alias_names(group)


def _contexts(group: GroupEvidence) -> set[str]:
    return {
        context
        for edition in group.editions
        for context in edition.context_terms
        if context
    }


def _title_similarity(left: GroupEvidence, right: GroupEvidence) -> float:
    return best_identity_similarity(
        _all_identity_names(left), _all_identity_names(right)
    )


def _cover_distances(left: GroupEvidence, right: GroupEvidence) -> list[int]:
    distances = {
        distance
        for left_edition in left.editions
        for right_edition in right.editions
        if (
            distance := cover_hash_distance(
                left_edition.cover_hash, right_edition.cover_hash
            )
        )
        is not None
    }
    return sorted(distances)


def _page_comparison(
    left: GroupEvidence, right: GroupEvidence
) -> tuple[int | None, float | None]:
    comparisons = [
        (abs(left_page - right_page), min(left_page, right_page) / max(left_page, right_page))
        for left_page in {item.page_count for item in left.editions if item.page_count}
        for right_page in {item.page_count for item in right.editions if item.page_count}
    ]
    if not comparisons:
        return None, None
    delta, ratio = min(comparisons, key=lambda item: (item[0], -item[1]))
    return delta, round(ratio, 5)


def _signals(
    left: GroupEvidence,
    right: GroupEvidence,
    title_score: float,
    cover_distances: list[int],
    page_delta: int | None,
    page_ratio: float | None,
    shared_terms: list[str],
) -> tuple[list[EvidenceCode], list[ConflictCode], list[ConflictCode], list[str]]:
    evidence: list[EvidenceCode] = []
    hard_conflicts: list[ConflictCode] = []
    soft_conflicts: list[ConflictCode] = []
    context_only: list[str] = []

    left_cores, right_cores = _core_names(left), _core_names(right)
    left_aliases, right_aliases = _alias_names(left), _alias_names(right)
    if left_cores & right_cores:
        evidence.append("core_title_match")
    elif (left_aliases & right_cores) or (right_aliases & left_cores):
        evidence.append("source_alias_match")
    elif title_score >= 0.68:
        evidence.append("core_title_similarity")
    elif title_score <= 0.62 and shared_terms:
        soft_conflicts.append("core_title_mismatch")

    left_numbers = {
        tuple(item.number_signature)
        for item in left.editions
        if item.number_signature
    }
    right_numbers = {
        tuple(item.number_signature)
        for item in right.editions
        if item.number_signature
    }
    if left_numbers and right_numbers:
        if left_numbers & right_numbers:
            evidence.append("number_match")
        else:
            hard_conflicts.append("number_mismatch")

    if cover_distances:
        minimum_cover_distance = min(cover_distances)
        if minimum_cover_distance <= 6:
            evidence.append("cover_hash_strong")
        elif minimum_cover_distance <= 10:
            evidence.append("cover_hash_weak")
        elif minimum_cover_distance >= 17:
            soft_conflicts.append("cover_dissimilar")

    left_pages = {item.page_count for item in left.editions if item.page_count is not None}
    right_pages = {item.page_count for item in right.editions if item.page_count is not None}
    if left_pages and right_pages and page_delta is not None and page_ratio is not None:
        maximum_pages = max(*left_pages, *right_pages)
        if page_delta <= max(2, round(maximum_pages * 0.03)):
            evidence.append("page_count_match")
        elif (page_delta >= 3 and page_ratio <= 0.65) or page_delta >= max(
            5, round(maximum_pages * 0.15)
        ):
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
            context_only.append("author_match")
        else:
            hard_conflicts.append("author_mismatch")

    left_providers = {source.provider for item in left.editions for source in item.sources}
    right_providers = {source.provider for item in right.editions for source in item.sources}
    if left_providers & right_providers:
        context_only.append("provider_overlap")
    if shared_terms:
        context_only.append("shared_series_or_context")

    left_variants = {label for item in left.editions for label in item.variant_labels}
    right_variants = {label for item in right.editions for label in item.variant_labels}
    if left_variants != right_variants and (left_variants or right_variants):
        context_only.append("variant_difference")
    left_languages = {item.language for item in left.editions if item.language}
    right_languages = {item.language for item in right.editions if item.language}
    if left_languages != right_languages and (left_languages or right_languages):
        context_only.append("language_difference")

    identity_support = set(evidence) & {
        "core_title_match",
        "source_alias_match",
        "core_title_similarity",
        "number_match",
        "cover_hash_strong",
        "cover_hash_weak",
        "page_count_match",
        "year_match",
    }
    if not identity_support or identity_support == {"core_title_similarity"}:
        soft_conflicts.append("insufficient_evidence")

    return (
        list(dict.fromkeys(evidence)),
        list(dict.fromkeys(hard_conflicts)),
        list(dict.fromkeys(soft_conflicts)),
        list(dict.fromkeys(context_only)),
    )

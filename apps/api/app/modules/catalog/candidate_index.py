from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass

from app.core.observability import record_metric
from app.db.models import Work, WorkFingerprint, WorkGroup
from app.modules.catalog.cover_fingerprint import (
    PreparedCoverFingerprint,
    compare_prepared_fingerprints,
    prepare_cover_fingerprint,
)
from app.modules.catalog.pair_identity import (
    group_anchor_info,
    is_anchor_pair_constrained,
)
from app.modules.catalog.title_identity import (
    PreparedIdentityText,
    TitleSemantics,
    best_prepared_similarity,
    extract_title_semantics,
    fold_cjk_variants,
    identity_names,
    prepare_identity_text,
)


def compact_text(value: str) -> str:
    return re.sub(r"\W+", "", unicodedata.normalize("NFKC", value).casefold())


def canonicalize_numbers(value: str) -> str:
    return re.sub(r"\d+", lambda match: str(int(match.group())), value)


def identity_number_signature(value: str) -> tuple[int, ...]:
    """Extract work numbers while ignoring common year and resolution noise."""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = re.sub(r"\b(?:19|20)\d{2}\b", " ", normalized)
    normalized = re.sub(r"\b\d{3,4}\s*[pk]\b", " ", normalized)
    numbers = (int(number) for number in re.findall(r"\d+", normalized))
    return tuple(dict.fromkeys(numbers))


def page_counts_match(left_pages: int | None, right_pages: int | None) -> bool:
    if left_pages is None or right_pages is None:
        return False
    tolerance = max(2, round(max(left_pages, right_pages) * 0.03))
    return abs(left_pages - right_pages) <= tolerance


def page_counts_compatible(left_pages: int | None, right_pages: int | None) -> bool:
    if left_pages is None or right_pages is None:
        return True
    return page_counts_match(left_pages, right_pages)


def page_count_conflict(left_pages: int | None, right_pages: int | None) -> bool:
    if left_pages is None or right_pages is None or left_pages <= 0 or right_pages <= 0:
        return False
    delta = abs(left_pages - right_pages)
    max_pages = max(left_pages, right_pages)
    ratio = min(left_pages, right_pages) / max_pages
    return (delta >= 3 and ratio <= 0.65) or delta >= max(5, round(max_pages * 0.15))


def extreme_page_mismatch(left_pages: int | None, right_pages: int | None) -> bool:
    if left_pages is None or right_pages is None or left_pages <= 0 or right_pages <= 0:
        return False
    delta = abs(left_pages - right_pages)
    ratio = min(left_pages, right_pages) / max(left_pages, right_pages)
    return delta >= 8 and ratio <= 0.45


def years_match(left_year: int | None, right_year: int | None) -> bool:
    return left_year is None or right_year is None or abs(left_year - right_year) <= 1


@dataclass(frozen=True, slots=True)
class PreparedWorkEntry:
    work_id: int
    fingerprint: WorkFingerprint
    primary_cores: frozenset[str]
    alias_cores: frozenset[str]
    folded_primary_cores: frozenset[str]
    folded_alias_cores: frozenset[str]
    prepared_primary: tuple[PreparedIdentityText, ...]
    prepared_folded_primary: tuple[PreparedIdentityText, ...]
    number_signature: tuple[int, ...]
    semantics: TitleSemantics
    identity_length: int
    page_count: int | None
    year: int | None
    cover: PreparedCoverFingerprint | None
    cover_dedup_key: tuple[object, ...] | None


@dataclass(frozen=True, slots=True)
class TitleCandidateHit:
    group: WorkGroup
    match_kind: str  # "primary_exact" | "alias_exact" | "folded_exact" | "fuzzy" | "folded_fuzzy"
    score: float
    matched_entry: PreparedWorkEntry


def build_prepared_work_entry(
    work: Work,
    fingerprint: WorkFingerprint,
) -> PreparedWorkEntry:
    primary_raw = {
        canonicalize_numbers(value)
        for value in identity_names(work.title, fingerprint.normalized_title, ())
    }
    primary_raw = {value for value in primary_raw if value}
    all_raw = {
        canonicalize_numbers(value)
        for value in identity_names(
            work.title, fingerprint.normalized_title, fingerprint.title_aliases or ()
        )
    }
    all_raw = {value for value in all_raw if value}
    alias_raw = all_raw - primary_raw

    folded_primary = frozenset(fold_cjk_variants(v) for v in primary_raw)
    folded_alias = frozenset(fold_cjk_variants(v) for v in alias_raw) - folded_primary

    prepared_primary = tuple(prepare_identity_text(v) for v in sorted(primary_raw))
    prepared_folded_primary = tuple(
        prepare_identity_text(v) for v in sorted(folded_primary)
    )

    cover: PreparedCoverFingerprint | None = None
    cover_dedup_key: tuple[object, ...] | None = None
    if fingerprint.cover_fingerprint is not None or fingerprint.cover_hash is not None:
        prepared_cover = prepare_cover_fingerprint(
            fingerprint.cover_fingerprint,
            legacy=fingerprint.cover_hash,
        )
        if not prepared_cover.is_low_entropy and (
            prepared_cover.variants or prepared_cover.legacy_hash is not None
        ):
            cover = prepared_cover
            cover_dedup_key = (
                prepared_cover.legacy_hash,
                tuple(
                    (item.is_full, round(item.log_ratio, 4), item.dhash, item.phash)
                    for item in prepared_cover.variants
                ),
            )

    return PreparedWorkEntry(
        work_id=work.id,
        fingerprint=fingerprint,
        primary_cores=frozenset(primary_raw),
        alias_cores=frozenset(alias_raw),
        folded_primary_cores=folded_primary,
        folded_alias_cores=folded_alias,
        prepared_primary=prepared_primary,
        prepared_folded_primary=prepared_folded_primary,
        number_signature=identity_number_signature(fingerprint.normalized_title),
        semantics=extract_title_semantics(work.title),
        identity_length=len(compact_text(fingerprint.normalized_title)),
        page_count=fingerprint.page_count,
        year=work.year,
        cover=cover,
        cover_dedup_key=cover_dedup_key,
    )


def _index_keys_for_prepared(items: tuple[PreparedIdentityText, ...]) -> set[str]:
    keys: set[str] = set()
    for item in items:
        keys.update(item.ngrams)
        for token in item.tokens:
            if len(token) == 1:
                keys.add(f"t:{token}")
    return keys


class AuthorCandidateIndex:
    """In-memory inverted & bucketed candidate index for one author's groups."""

    def __init__(self, constrained_keys: set[str] | None = None) -> None:
        self.constrained_keys: set[str] = set(constrained_keys or ())
        self.groups: dict[int, WorkGroup] = {}
        self.entries_by_group: dict[int, list[PreparedWorkEntry]] = {}
        self.group_has_manual: dict[int, bool] = {}
        self.group_numbers: dict[int, frozenset[tuple[int, ...]]] = {}
        self.group_semantics: dict[int, frozenset[TitleSemantics]] = {}
        self.group_anchors: dict[int, tuple[str, tuple[str, ...]]] = {}
        self.group_cover_reps: dict[int, tuple[PreparedWorkEntry, ...]] = {}

        self.by_primary_core: dict[str, set[int]] = defaultdict(set)
        self.by_alias_core: dict[str, set[int]] = defaultdict(set)
        self.by_folded_core: dict[str, set[int]] = defaultdict(set)
        self.by_number: dict[tuple[int, ...], set[int]] = defaultdict(set)
        self.by_ngram: dict[str, set[int]] = defaultdict(set)
        self.by_folded_ngram: dict[str, set[int]] = defaultdict(set)
        self._group_tokens: dict[int, dict[str, set[object]]] = {}

    def add_group(
        self,
        group: WorkGroup,
        entry_loader: Callable[[Work, WorkFingerprint], PreparedWorkEntry],
    ) -> None:
        gid = group.id
        if gid is None:
            return
        if gid in self.groups:
            self.remove_group(gid)

        entries: list[PreparedWorkEntry] = []
        primary_keys: set[str] = set()
        alias_keys: set[str] = set()
        folded_keys: set[str] = set()
        numbers: set[tuple[int, ...]] = set()
        semantics: set[TitleSemantics] = set()
        ngram_keys: set[str] = set()
        folded_ngram_keys: set[str] = set()
        cover_reps: list[PreparedWorkEntry] = []
        seen_covers: set[tuple[object, int | None, int | None]] = set()

        for member in group.members:
            work = member.work
            fp = work.fingerprint
            if fp is None:
                continue
            entry = entry_loader(work, fp)
            entries.append(entry)
            numbers.add(entry.number_signature)
            semantics.add(entry.semantics)
            for value in entry.primary_cores:
                if len(compact_text(value)) >= 3:
                    primary_keys.add(value)
            for value in entry.alias_cores:
                if len(compact_text(value)) >= 3:
                    alias_keys.add(value)
            for value in entry.folded_primary_cores | entry.folded_alias_cores:
                if len(compact_text(value)) >= 3:
                    folded_keys.add(value)
            ngram_keys.update(_index_keys_for_prepared(entry.prepared_primary))
            folded_ngram_keys.update(
                _index_keys_for_prepared(entry.prepared_folded_primary)
            )
            if entry.cover is not None and entry.cover_dedup_key is not None:
                dedup_sig = (entry.cover_dedup_key, entry.page_count, entry.year)
                if dedup_sig not in seen_covers:
                    seen_covers.add(dedup_sig)
                    cover_reps.append(entry)

        self.groups[gid] = group
        self.entries_by_group[gid] = entries
        self.group_has_manual[gid] = any(member.is_manual for member in group.members)
        self.group_numbers[gid] = frozenset(numbers)
        self.group_semantics[gid] = frozenset(semantics)
        self.group_anchors[gid] = group_anchor_info(group)
        self.group_cover_reps[gid] = tuple(cover_reps)

        for key in primary_keys:
            self.by_primary_core[key].add(gid)
        for key in alias_keys:
            self.by_alias_core[key].add(gid)
        for key in folded_keys:
            self.by_folded_core[key].add(gid)
        for num in numbers:
            self.by_number[num].add(gid)
        for key in ngram_keys:
            self.by_ngram[key].add(gid)
        for key in folded_ngram_keys:
            self.by_folded_ngram[key].add(gid)

        self._group_tokens[gid] = {
            "primary": set(primary_keys),
            "alias": set(alias_keys),
            "folded": set(folded_keys),
            "number": set(numbers),
            "ngram": set(ngram_keys),
            "folded_ngram": set(folded_ngram_keys),
        }

    def remove_group(self, gid: int) -> None:
        if gid not in self.groups:
            return
        self.groups.pop(gid, None)
        self.entries_by_group.pop(gid, None)
        self.group_has_manual.pop(gid, None)
        self.group_numbers.pop(gid, None)
        self.group_semantics.pop(gid, None)
        self.group_anchors.pop(gid, None)
        self.group_cover_reps.pop(gid, None)

        tokens = self._group_tokens.pop(gid, None)
        if not tokens:
            return
        bucket_map = (
            ("primary", self.by_primary_core),
            ("alias", self.by_alias_core),
            ("folded", self.by_folded_core),
            ("number", self.by_number),
            ("ngram", self.by_ngram),
            ("folded_ngram", self.by_folded_ngram),
        )
        for name, index in bucket_map:
            for key in tokens[name]:
                bucket = index.get(key)
                if bucket is not None:
                    bucket.discard(gid)
                    if not bucket:
                        index.pop(key, None)

    def is_constrained(
        self,
        left_primary_anchor: str,
        left_member_anchors: tuple[str, ...],
        right_gid: int,
    ) -> bool:
        if not self.constrained_keys:
            return False
        right_info = self.group_anchors.get(right_gid)
        if right_info is None:
            return False
        return is_anchor_pair_constrained(
            left_primary_anchor,
            left_member_anchors,
            right_info[0],
            right_info[1],
            self.constrained_keys,
        )

    def _group_passes_gates(
        self,
        entry: PreparedWorkEntry,
        gid: int,
        left_primary_anchor: str,
        left_member_anchors: tuple[str, ...],
        *,
        excluding_group_id: int | None = None,
        allow_manual: bool = True,
        allow_one_sided_number: bool = False,
    ) -> tuple[bool, bool]:
        if excluding_group_id is not None and gid == excluding_group_id:
            return False, False
        if not allow_manual and self.group_has_manual.get(gid, False):
            return False, False
        nums = self.group_numbers.get(gid)
        if not nums:
            return False, False
        one_sided = False
        if any(num != entry.number_signature for num in nums):
            if not allow_one_sided_number:
                return False, False
            for num in nums:
                if num == entry.number_signature:
                    continue
                if bool(num) == bool(entry.number_signature):
                    return False, False
                one_sided = True
        sems = self.group_semantics.get(gid, frozenset())
        if any(entry.semantics.conflicts_with(sem) for sem in sems):
            return False, False
        if self.is_constrained(left_primary_anchor, left_member_anchors, gid):
            return False, False
        return True, one_sided

    def best_title_candidate(
        self,
        entry: PreparedWorkEntry,
        left_primary_anchor: str,
        left_member_anchors: tuple[str, ...],
        *,
        excluding_group_id: int | None = None,
        skip_group_ids: set[int] | None = None,
        allow_manual: bool = True,
        min_score: float = 0.68,
    ) -> TitleCandidateHit | None:
        hits = self.title_candidates(
            entry,
            left_primary_anchor,
            left_member_anchors,
            excluding_group_id=excluding_group_id,
            skip_group_ids=skip_group_ids,
            allow_manual=allow_manual,
            min_score=min_score,
            top_k=1,
        )
        return hits[0] if hits else None

    def title_candidates(
        self,
        entry: PreparedWorkEntry,
        left_primary_anchor: str,
        left_member_anchors: tuple[str, ...],
        *,
        excluding_group_id: int | None = None,
        skip_group_ids: set[int] | None = None,
        allow_manual: bool = True,
        min_score: float = 0.68,
        fuzzy_cover_threshold: float = 0.72,
        top_k: int = 5,
    ) -> list[TitleCandidateHit]:
        """Return exact hit (if any) or top-k fuzzy title hits so cover matches aren't shadowed."""
        # Exact tiers return single deterministic best hit.
        for kind, raw_keys in (
            ("primary_exact", entry.primary_cores),
            ("alias_exact", entry.primary_cores | entry.alias_cores),
            ("folded_exact", entry.folded_primary_cores | entry.folded_alias_cores),
        ):
            gids: set[int] = set()
            for value in raw_keys:
                if len(compact_text(value)) < 3:
                    continue
                if kind == "primary_exact":
                    gids.update(self.by_primary_core.get(value, ()))
                elif kind == "alias_exact":
                    gids.update(self.by_primary_core.get(value, ()))
                    gids.update(self.by_alias_core.get(value, ()))
                else:
                    gids.update(self.by_folded_core.get(value, ()))
            if skip_group_ids:
                gids.difference_update(skip_group_ids)
            hit = self._pick_exact_hit(
                entry,
                gids,
                kind,
                left_primary_anchor,
                left_member_anchors,
                excluding_group_id=excluding_group_id,
                allow_manual=allow_manual,
            )
            if hit is not None:
                return [hit]

        number_bucket = self.by_number.get(entry.number_signature)
        if not number_bucket:
            return []

        raw_candidates: set[int] = set()
        for key in _index_keys_for_prepared(entry.prepared_primary):
            posting = self.by_ngram.get(key)
            if posting:
                raw_candidates.update(posting & number_bucket)

        folded_candidates: set[int] = set()
        for key in _index_keys_for_prepared(entry.prepared_folded_primary):
            posting = self.by_folded_ngram.get(key)
            if posting:
                folded_candidates.update(posting & number_bucket)

        all_fuzzy = raw_candidates | folded_candidates
        if skip_group_ids:
            all_fuzzy.difference_update(skip_group_ids)
        if not all_fuzzy:
            return []

        cutoff = max(0.0, min_score - 1e-5)
        hits: list[TitleCandidateHit] = []
        for gid in sorted(all_fuzzy):
            ok, _ = self._group_passes_gates(
                entry,
                gid,
                left_primary_anchor,
                left_member_anchors,
                excluding_group_id=excluding_group_id,
                allow_manual=allow_manual,
            )
            if not ok:
                continue
            group = self.groups[gid]
            best_g_score = 0.0
            best_g_entry: PreparedWorkEntry | None = None
            best_g_kind = "fuzzy"
            for other in self.entries_by_group[gid]:
                record_metric("candidate_comparisons")
                g_cutoff = best_g_score if best_g_score > cutoff else cutoff
                raw_score = (
                    best_prepared_similarity(
                        entry.prepared_primary,
                        other.prepared_primary,
                        min_score=g_cutoff,
                    )
                    if gid in raw_candidates
                    else 0.0
                )
                folded_score = (
                    best_prepared_similarity(
                        entry.prepared_folded_primary,
                        other.prepared_folded_primary,
                        min_score=g_cutoff,
                    )
                    if gid in folded_candidates
                    else 0.0
                )
                score = raw_score
                kind = "fuzzy"
                if folded_score > raw_score and folded_score > best_g_score:
                    score = folded_score
                    kind = "folded_fuzzy"
                if score > best_g_score:
                    best_g_score = score
                    best_g_entry = other
                    best_g_kind = kind
            if best_g_entry is not None and best_g_score >= min_score:
                hits.append(
                    TitleCandidateHit(
                        group=group,
                        match_kind=best_g_kind,
                        score=best_g_score,
                        matched_entry=best_g_entry,
                    )
                )

        if not hits:
            return []
        hits.sort(key=lambda h: (-h.score, h.group.id))
        if top_k <= 1 or entry.cover is None:
            return [hits[0]]
        filtered = [hits[0]]
        for item in hits[1:]:
            if item.score < fuzzy_cover_threshold or len(filtered) >= top_k:
                break
            filtered.append(item)
        return filtered

    def _pick_exact_hit(
        self,
        entry: PreparedWorkEntry,
        gids: set[int],
        match_kind: str,
        left_primary_anchor: str,
        left_member_anchors: tuple[str, ...],
        *,
        excluding_group_id: int | None,
        allow_manual: bool,
    ) -> TitleCandidateHit | None:
        if not gids:
            return None
        best_rank: tuple[int, int, int] | None = None
        best_hit: TitleCandidateHit | None = None
        for gid in sorted(gids):
            ok, _ = self._group_passes_gates(
                entry,
                gid,
                left_primary_anchor,
                left_member_anchors,
                excluding_group_id=excluding_group_id,
                allow_manual=allow_manual,
            )
            if not ok:
                continue
            group = self.groups[gid]
            for other in self.entries_by_group[gid]:
                record_metric("candidate_comparisons")
                matched = False
                if match_kind == "primary_exact":
                    matched = any(
                        len(compact_text(v)) >= 3
                        for v in (entry.primary_cores & other.primary_cores)
                    )
                elif match_kind == "alias_exact":
                    left_all = entry.primary_cores | entry.alias_cores
                    right_all = other.primary_cores | other.alias_cores
                    matched = any(
                        len(compact_text(v)) >= 3 for v in (left_all & right_all)
                    )
                else:
                    left_fold = entry.folded_primary_cores | entry.folded_alias_cores
                    right_fold = other.folded_primary_cores | other.folded_alias_cores
                    matched = any(
                        len(compact_text(v)) >= 3 for v in (left_fold & right_fold)
                    )
                if not matched:
                    continue
                is_clean_pages = (
                    0
                    if (
                        not extreme_page_mismatch(entry.page_count, other.page_count)
                        and not page_count_conflict(entry.page_count, other.page_count)
                    )
                    else 1
                )
                is_year_match = 0 if years_match(entry.year, other.year) else 1
                rank = (is_clean_pages, is_year_match, gid)
                if best_rank is None or rank < best_rank:
                    best_rank = rank
                    best_hit = TitleCandidateHit(
                        group=group,
                        match_kind=match_kind,
                        score=1.0,
                        matched_entry=other,
                    )
        return best_hit

    def best_cover_for_group(
        self,
        entry: PreparedWorkEntry,
        gid: int,
        *,
        max_distance: int | None = None,
    ) -> tuple[PreparedWorkEntry | None, int | None, str | None]:
        if entry.cover is None:
            return None, None, None
        reps = self.group_cover_reps.get(gid, ())
        if not reps:
            return None, None, None
        best_rep: PreparedWorkEntry | None = None
        best_dist: int | None = None
        best_mode: str | None = None
        best_rank: tuple[int, int, int] | None = None
        for rep in reps:
            if rep.cover is None:
                continue
            record_metric("candidate_comparisons")
            cutoff = (
                best_dist
                if best_dist is not None and (max_distance is None or best_dist < max_distance)
                else max_distance
            )
            comparison = compare_prepared_fingerprints(
                entry.cover, rep.cover, max_distance=cutoff
            )
            if comparison is None:
                continue
            compat_penalty = (
                0
                if (
                    years_match(entry.year, rep.year)
                    and page_counts_compatible(entry.page_count, rep.page_count)
                )
                else 1
            )
            rank = (comparison.distance, compat_penalty, rep.work_id)
            if best_rank is None or rank < best_rank:
                best_rank = rank
                best_rep = rep
                best_dist = comparison.distance
                best_mode = comparison.mode
        return best_rep, best_dist, best_mode

    def find_cover_candidates(
        self,
        entry: PreparedWorkEntry,
        left_primary_anchor: str,
        left_member_anchors: tuple[str, ...],
        *,
        excluding_group_id: int | None = None,
        skip_group_ids: set[int] | None = None,
        allow_manual_auto: bool = True,
        strong_distance: int = 8,
        suggest_distance: int = 13,
    ) -> tuple[
        tuple[WorkGroup, PreparedWorkEntry, int, str] | None,
        tuple[WorkGroup, PreparedWorkEntry, int, str, bool] | None,
    ]:
        """Find best strong auto-merge cover candidate AND best suggestion candidate in one pass."""
        if entry.cover is None:
            return None, None
        candidate_gids: set[int] = set(self.by_number.get(entry.number_signature, ()))
        if entry.number_signature:
            candidate_gids.update(self.by_number.get((), ()))
        else:
            for num, gids in self.by_number.items():
                if num:
                    candidate_gids.update(gids)
        if skip_group_ids:
            candidate_gids.difference_update(skip_group_ids)
        if not candidate_gids:
            return None, None

        auto_best: tuple[WorkGroup, PreparedWorkEntry, int, str] | None = None
        auto_dist: int | None = None
        sug_best: tuple[WorkGroup, PreparedWorkEntry, int, str, bool] | None = None
        sug_dist: int | None = None

        for gid in sorted(candidate_gids):
            ok, one_sided = self._group_passes_gates(
                entry,
                gid,
                left_primary_anchor,
                left_member_anchors,
                excluding_group_id=excluding_group_id,
                allow_manual=True,
                allow_one_sided_number=True,
            )
            if not ok:
                continue
            reps = self.group_cover_reps.get(gid, ())
            if not reps:
                continue
            group = self.groups[gid]
            can_auto_group = (
                not one_sided
                and (allow_manual_auto or not self.group_has_manual.get(gid, False))
            )
            max_allowed = strong_distance if one_sided else suggest_distance
            for rep in reps:
                if rep.cover is None:
                    continue
                if one_sided and extreme_page_mismatch(entry.page_count, rep.page_count):
                    continue
                record_metric("candidate_comparisons")
                need_sug = sug_dist if sug_dist is not None else max_allowed
                rep_can_auto = (
                    can_auto_group
                    and years_match(entry.year, rep.year)
                    and page_counts_compatible(entry.page_count, rep.page_count)
                )
                if rep_can_auto:
                    need_auto = auto_dist if auto_dist is not None else strong_distance
                    cutoff = need_sug if need_sug >= need_auto else need_auto
                else:
                    cutoff = need_sug
                if cutoff > max_allowed:
                    cutoff = max_allowed
                comparison = compare_prepared_fingerprints(
                    entry.cover, rep.cover, max_distance=cutoff
                )
                if comparison is None:
                    continue
                dist = comparison.distance
                mode = comparison.mode
                if (
                    rep_can_auto
                    and dist <= strong_distance
                    and (mode != "crop" or page_counts_match(entry.page_count, rep.page_count))
                ):
                    if auto_dist is None or dist < auto_dist:
                        auto_dist = dist
                        auto_best = (group, rep, dist, mode)
                if dist <= max_allowed and (sug_dist is None or dist < sug_dist):
                    sug_dist = dist
                    sug_best = (group, rep, dist, mode, one_sided)

        return auto_best, sug_best

    def best_cover_candidate(
        self,
        entry: PreparedWorkEntry,
        left_primary_anchor: str,
        left_member_anchors: tuple[str, ...],
        *,
        excluding_group_id: int | None = None,
        allow_manual: bool = True,
        max_distance: int = 13,
        require_year_page: bool = False,
        allow_one_sided_number: bool = False,
    ) -> tuple[WorkGroup | None, PreparedWorkEntry | None, int | None, str | None, bool]:
        if entry.cover is None:
            return None, None, None, None, False
        candidate_gids: set[int] = set(self.by_number.get(entry.number_signature, ()))
        if allow_one_sided_number:
            if entry.number_signature:
                candidate_gids.update(self.by_number.get((), ()))
            else:
                for num, gids in self.by_number.items():
                    if num:
                        candidate_gids.update(gids)
        if not candidate_gids:
            return None, None, None, None, False

        best_group: WorkGroup | None = None
        best_rep: PreparedWorkEntry | None = None
        best_distance: int | None = None
        best_mode: str | None = None
        best_one_sided = False

        for gid in sorted(candidate_gids):
            ok, one_sided = self._group_passes_gates(
                entry,
                gid,
                left_primary_anchor,
                left_member_anchors,
                excluding_group_id=excluding_group_id,
                allow_manual=allow_manual,
                allow_one_sided_number=allow_one_sided_number,
            )
            if not ok:
                continue
            reps = self.group_cover_reps.get(gid, ())
            if not reps:
                continue
            group = self.groups[gid]
            for rep in reps:
                if rep.cover is None:
                    continue
                if require_year_page and (
                    not years_match(entry.year, rep.year)
                    or not page_counts_compatible(entry.page_count, rep.page_count)
                ):
                    continue
                record_metric("candidate_comparisons")
                cutoff = (
                    best_distance
                    if best_distance is not None and best_distance < max_distance
                    else max_distance
                )
                comparison = compare_prepared_fingerprints(
                    entry.cover, rep.cover, max_distance=cutoff
                )
                if comparison is None:
                    continue
                if comparison.mode == "crop" and require_year_page and not page_counts_match(
                    entry.page_count, rep.page_count
                ):
                    continue
                if best_distance is None or comparison.distance < best_distance:
                    best_group = group
                    best_rep = rep
                    best_distance = comparison.distance
                    best_mode = comparison.mode
                    best_one_sided = one_sided

        return best_group, best_rep, best_distance, best_mode, best_one_sided

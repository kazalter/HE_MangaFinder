from __future__ import annotations

import asyncio
import html
import io
import re
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx
from PIL import Image, UnidentifiedImageError
from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session, selectinload

from app.core.observability import record_metric
from app.core.time import as_utc
from app.db.models import (
    Author,
    AuthorWork,
    MergeSuggestion,
    PairConstraint,
    Work,
    WorkFingerprint,
    WorkGroup,
    WorkGroupMember,
)
from app.modules.catalog.candidate_index import (
    AuthorCandidateIndex,
    PreparedWorkEntry,
    TitleCandidateHit,
    build_prepared_work_entry,
    canonicalize_numbers,
    compact_text,
    extreme_page_mismatch,
    identity_number_signature,
    page_count_conflict,
    page_counts_compatible,
    page_counts_match,
    years_match,
)
from app.modules.catalog.cover_fingerprint import (
    compare_fingerprints,
    fingerprint_image,
    hash_distance,
    is_current_fingerprint,
)
from app.modules.catalog.group_repository import WorkGroupRepository
from app.modules.catalog.invalidation import (
    authors_for_groups,
    authors_for_works,
    bump_authors_for_groups,
    bump_authors_for_works,
    capture_author_revision,
    complete_author_revision,
)
from app.modules.catalog.pair_identity import (
    candidate_key,
    group_anchor_info,
    is_anchor_pair_constrained,
    work_source_anchor,
)
from app.modules.catalog.title_identity import (
    VERSION_PATTERNS,
    best_identity_similarity,
    extract_title_semantics,
    identity_names,
    iter_top_level_brackets,
    normalize_title,
    parse_title_identity,
)
from app.providers.errors import ProviderError

if TYPE_CHECKING:
    from app.providers.registry import ProviderRegistry

_TITLE_FUZZY_THRESHOLD = 0.94
_TITLE_WITH_COVER_THRESHOLD = 0.72
_TITLE_WITH_COVER_DISTANCE = 10
_STRONG_COVER_DISTANCE = 8
_SUGGEST_COVER_DISTANCE = 13

_compact = compact_text
_canonicalize_numbers = canonicalize_numbers


def extract_variant_labels(title: str, language: str | None = None) -> list[str]:
    import unicodedata

    value = unicodedata.normalize("NFKC", html.unescape(title))
    labels: list[str] = []
    for pattern, label in VERSION_PATTERNS:
        if re.search(pattern, value, re.IGNORECASE) and label not in labels:
            labels.append(label)
    for span in iter_top_level_brackets(value):
        content = " ".join(span.content.split())
        if re.search(r"汉化|漢化|翻译|翻譯|翻訳", content, re.IGNORECASE):
            if content not in labels:
                labels.append(content)
    language_labels = {
        "zh-hans": "简体中文",
        "zh-hant": "繁体中文",
        "zh": "中文",
        "ja": "日文",
        "en": "英文",
        "ko": "韩文",
    }
    if language and (label := language_labels.get(language.casefold())):
        labels.append(label)
    return labels


def cover_hash_distance(left: str | None, right: str | None) -> int | None:
    """Legacy 64-bit dHash distance kept for API and old database compatibility."""
    return hash_distance(left, right)


def cover_fingerprint_distance(
    left: WorkFingerprint, right: WorkFingerprint
) -> tuple[int | None, str | None, bool]:
    comparison = compare_fingerprints(
        left.cover_fingerprint,
        right.cover_fingerprint,
        left_legacy=left.cover_hash,
        right_legacy=right.cover_hash,
    )
    if comparison is None:
        return None, None, False
    return comparison.distance, comparison.mode, comparison.reliable_negative


def _fingerprint_image_bytes(data: bytes) -> dict[str, Any]:
    with Image.open(io.BytesIO(data)) as image:
        return fingerprint_image(image)


class CoverHasher:
    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        providers: ProviderRegistry | None = None,
    ) -> None:
        self._owns_client = client is None
        self._providers = providers
        self._client = client or httpx.AsyncClient(
            headers={"User-Agent": "MangaFinder/0.1 cover-fingerprint"},
            follow_redirects=True,
            timeout=httpx.Timeout(15.0),
            limits=httpx.Limits(max_connections=3),
        )

    async def hash_url(self, url: str | None) -> str | None:
        fingerprint = await self.fingerprint_url(url)
        if not fingerprint:
            return None
        variants = fingerprint.get("variants", [])
        return variants[0].get("dhash") if variants else None

    async def fingerprint_url(
        self,
        url: str | None,
        provider_name: str | None = None,
        external_id: str | None = None,
    ) -> dict[str, Any] | None:
        if not url or not url.startswith(("http://", "https://")):
            return None
        if re.search(r"placeholder|no[-_]?cover|default[-_]?cover", url, re.IGNORECASE):
            return None
        try:
            content: bytes | bytearray
            provider = (
                self._providers.get_optional(provider_name)
                if self._providers and provider_name
                else None
            )
            if provider and external_id:
                remote = await provider.fetch_cover(external_id, url)
                content = remote.content
                if len(content) > 6 * 1024 * 1024:
                    return None
            else:
                streamed = bytearray()
                async with self._client.stream("GET", url) as response:
                    response.raise_for_status()
                    async for chunk in response.aiter_bytes():
                        streamed.extend(chunk)
                        if len(streamed) > 6 * 1024 * 1024:
                            return None
                content = streamed
            return await asyncio.to_thread(_fingerprint_image_bytes, bytes(content))
        except (
            httpx.HTTPError,
            Image.DecompressionBombError,
            OSError,
            ProviderError,
            UnidentifiedImageError,
            ValueError,
        ):
            return None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class AggregationService:
    def __init__(self, session: Session, cover_hasher: CoverHasher | None = None) -> None:
        self.session = session
        self.cover_hasher = cover_hasher
        self._batch_groups: dict[int, dict[int, WorkGroup]] = {}
        self._batch_indexes: dict[int, AuthorCandidateIndex] = {}
        self._batch_constrained_keys: dict[int, set[str]] = {}
        self._batch_suggestions: dict[int, dict[tuple[int, int], MergeSuggestion]] = {}
        self._batch_dirty_recompute: dict[int, set[int]] = {}
        self._batch_mod_seq: dict[int, dict[int, int]] = {}
        self._batch_done_seq: dict[int, dict[int, int]] = {}
        self._batch_seq: int = 0
        self._batch_defer_recompute: bool = False
        self._active_author_id: int | None = None
        self._entry_cache: dict[int, PreparedWorkEntry] = {}
        self._fp_primary_names_cache: dict[int, set[str]] = {}
        self._fp_num_sig_cache: dict[int, tuple[int, ...]] = {}

    def begin_author_batch(self, author_id: int) -> list[WorkGroup]:
        """Preload candidate groups, constraints, suggestions, and build index for an author."""
        if author_id in self._batch_groups:
            return list(self._batch_groups[author_id].values())
        record_metric("candidate_loads")
        work_load = selectinload(WorkGroup.members).selectinload(WorkGroupMember.work)
        groups = list(
            self.session.scalars(
                select(WorkGroup)
                .join(WorkGroupMember)
                .join(AuthorWork, AuthorWork.work_id == WorkGroupMember.work_id)
                .where(AuthorWork.author_id == author_id)
                .options(
                    work_load.selectinload(Work.fingerprint),
                    work_load.selectinload(Work.sources),
                )
                .distinct()
            )
        )
        constrained_keys = set(
            self.session.scalars(
                select(PairConstraint.candidate_key).where(
                    PairConstraint.decision == "different_work"
                )
            )
        )
        suggestions_map: dict[tuple[int, int], MergeSuggestion] = {}
        group_ids = [group.id for group in groups if group.id is not None]
        if group_ids:
            for offset in range(0, len(group_ids), 400):
                chunk = group_ids[offset : offset + 400]
                rows = self.session.scalars(
                    select(MergeSuggestion).where(
                        or_(
                            MergeSuggestion.source_group_id.in_(chunk),
                            MergeSuggestion.target_group_id.in_(chunk),
                        )
                    )
                )
                for row in rows:
                    key = (
                        min(row.source_group_id, row.target_group_id),
                        max(row.source_group_id, row.target_group_id),
                    )
                    suggestions_map[key] = row

        index = AuthorCandidateIndex(constrained_keys)
        for group in groups:
            index.add_group(group, self._cached_entry)

        self._batch_groups[author_id] = {group.id: group for group in groups}
        self._batch_indexes[author_id] = index
        self._batch_constrained_keys[author_id] = constrained_keys
        self._batch_suggestions[author_id] = suggestions_map
        self._batch_dirty_recompute[author_id] = set()
        self._batch_mod_seq[author_id] = {
            group.id: 0 for group in groups if group.id is not None
        }
        self._batch_done_seq[author_id] = {}
        return groups

    def end_author_batch(self, author_id: int) -> None:
        self._batch_groups.pop(author_id, None)
        self._batch_indexes.pop(author_id, None)
        self._batch_constrained_keys.pop(author_id, None)
        self._batch_suggestions.pop(author_id, None)
        self._batch_dirty_recompute.pop(author_id, None)
        self._batch_mod_seq.pop(author_id, None)
        self._batch_done_seq.pop(author_id, None)
        if not self._batch_groups:
            self._entry_cache.clear()
            self._fp_primary_names_cache.clear()
            self._fp_num_sig_cache.clear()

    def _invalidate_fp_cache(self, work_id: int | None) -> None:
        if work_id is None:
            return
        self._entry_cache.pop(work_id, None)
        self._fp_primary_names_cache.pop(work_id, None)
        self._fp_num_sig_cache.pop(work_id, None)

    def _touch_batch_group(self, author_id: int, group_id: int) -> None:
        self._batch_seq += 1
        if mod_map := self._batch_mod_seq.get(author_id):
            mod_map[group_id] = self._batch_seq
        if done_map := self._batch_done_seq.get(author_id):
            done_map.pop(group_id, None)

    def _cache_add_group(self, group: WorkGroup) -> None:
        if group.id is None or not self._batch_groups:
            return
        if self._active_author_id is not None and self._active_author_id in self._batch_groups:
            self._batch_groups[self._active_author_id][group.id] = group
            if index := self._batch_indexes.get(self._active_author_id):
                index.add_group(group, self._cached_entry)
            self._touch_batch_group(self._active_author_id, group.id)
            return
        member_authors = authors_for_groups(self.session, [group.id])
        for aid in member_authors:
            if aid in self._batch_groups:
                self._batch_groups[aid][group.id] = group
                if index := self._batch_indexes.get(aid):
                    index.add_group(group, self._cached_entry)
                self._touch_batch_group(aid, group.id)

    def _cache_remove_group(self, group_id: int) -> None:
        for aid, mapping in self._batch_groups.items():
            mapping.pop(group_id, None)
            if index := self._batch_indexes.get(aid):
                index.remove_group(group_id)
            if dirty := self._batch_dirty_recompute.get(aid):
                dirty.discard(group_id)
            if mod_map := self._batch_mod_seq.get(aid):
                mod_map.pop(group_id, None)
            if done_map := self._batch_done_seq.get(aid):
                done_map.pop(group_id, None)

    def _cached_entry(self, work: Work, fingerprint: WorkFingerprint) -> PreparedWorkEntry:
        work_id = work.id
        if work_id is not None and work_id in self._entry_cache:
            return self._entry_cache[work_id]
        entry = build_prepared_work_entry(work, fingerprint)
        if work_id is not None and self._batch_groups:
            self._entry_cache[work_id] = entry
        return entry

    def _ephemeral_index(
        self, author_id: int, candidates: Sequence[WorkGroup]
    ) -> AuthorCandidateIndex:
        if author_id in self._batch_indexes:
            return self._batch_indexes[author_id]
        constrained_keys = set(
            self.session.scalars(
                select(PairConstraint.candidate_key).where(
                    PairConstraint.decision == "different_work"
                )
            )
        )
        index = AuthorCandidateIndex(constrained_keys)
        for group in candidates:
            index.add_group(group, self._cached_entry)
        return index

    @staticmethod
    def _author_names_for_normalization(author: Author) -> tuple[str, ...]:
        names = [author.name, *(author.aliases or ()), *(author.circle_names or ())]
        return tuple(dict.fromkeys(name for name in names if name))

    async def reconcile_author(
        self,
        author: Author,
        *,
        works: Sequence[Work] | None = None,
    ) -> int:
        """Aggregate/reconcile works for `author` using a single preloaded candidate index."""
        if works is None:
            target_works = list(
                self.session.scalars(
                    select(Work)
                    .join(AuthorWork, AuthorWork.work_id == Work.id)
                    .where(AuthorWork.author_id == author.id)
                    .order_by(Work.id)
                )
            )
        else:
            seen_ids: set[int] = set()
            target_works = []
            for item in works:
                if item.id not in seen_ids:
                    seen_ids.add(item.id)
                    target_works.append(item)
            ungrouped = list(
                self.session.scalars(
                    select(Work)
                    .join(AuthorWork, AuthorWork.work_id == Work.id)
                    .outerjoin(WorkGroupMember, WorkGroupMember.work_id == Work.id)
                    .where(
                        AuthorWork.author_id == author.id,
                        WorkGroupMember.work_id.is_(None),
                    )
                    .order_by(Work.id)
                )
            )
            for item in ungrouped:
                if item.id not in seen_ids:
                    seen_ids.add(item.id)
                    target_works.append(item)

        if not target_works:
            return 0

        self.begin_author_batch(author.id)
        prev_active = self._active_author_id
        prev_defer = self._batch_defer_recompute
        self._active_author_id = author.id
        self._batch_defer_recompute = works is None
        remaining_by_group: dict[int, int] = {}
        if self._batch_defer_recompute:
            for item in target_works:
                if item.group_membership is not None:
                    gid = item.group_membership.group_id
                    remaining_by_group[gid] = remaining_by_group.get(gid, 0) + 1
        processed = 0
        try:
            for item in target_works:
                orig_gid = (
                    item.group_membership.group_id
                    if self._batch_defer_recompute and item.group_membership is not None
                    else None
                )
                result_group = await self.assign(item, author)
                if orig_gid is not None and orig_gid in remaining_by_group:
                    remaining_by_group[orig_gid] -= 1
                    if remaining_by_group[orig_gid] == 0 and result_group.id == orig_gid:
                        index = self._batch_indexes.get(author.id)
                        if (
                            index is not None
                            and not index.group_has_manual.get(orig_gid, False)
                            and not result_group.manual_metadata
                        ):
                            self._batch_seq += 1
                            self._batch_done_seq[author.id][orig_gid] = self._batch_seq
                processed += 1
                if processed % 10 == 0:
                    await asyncio.sleep(0)
            if self._batch_defer_recompute:
                dirty_ids = sorted(self._batch_dirty_recompute.get(author.id, ()))
                groups_map = self._batch_groups.get(author.id, {})
                for gid in dirty_ids:
                    if group := groups_map.get(gid):
                        self.recompute(group)
        finally:
            self._batch_defer_recompute = prev_defer
            self._active_author_id = prev_active
            self.end_author_batch(author.id)
        return processed

    async def reaggregate_author(self, author: Author) -> bool:
        """Forced full reconciliation entry point with atomic revision completion."""
        snapshot_revision = capture_author_revision(
            self.session, author.id, reason="manual_force"
        )
        await self.reconcile_author(author)
        self.prune_pending_suggestions(author.id)
        return complete_author_revision(self.session, author.id, snapshot_revision)

    async def assign(self, work: Work, author: Author) -> WorkGroup:
        record_metric("aggregation_executed")
        author_names = self._author_names_for_normalization(author)
        fingerprint, fp_changed = self._upsert_fingerprint_checked(work, author_names)
        cover_changed = False
        if self.cover_hasher:
            cover_changed = await self._ensure_cover_fingerprint(fingerprint, work.cover_url)
            if cover_changed:
                self._invalidate_fp_cache(work.id)

        entry = self._cached_entry(work, fingerprint)
        membership = work.group_membership
        if (
            membership
            and not any(member.is_manual for member in membership.group.members)
            and not membership.group.manual_metadata
            and not self._group_consistency_matches(
                entry,
                membership.group,
                excluding_work_id=work.id,
                constrained_keys=self._batch_constrained_keys.get(author.id),
            )
        ):
            self._detach_automatic_membership(membership)
            membership = None

        if membership:
            group = membership.group
            if fp_changed or cover_changed:
                self._cache_add_group(group)
            merged_or_same = group
            if not any(member.is_manual for member in group.members) and not group.manual_metadata:
                merged_or_same = await self._reconcile_existing(
                    work, author, entry, group, allow_cover_fetch=bool(self.cover_hasher)
                )
            if self._batch_defer_recompute and self._active_author_id == author.id:
                if fp_changed or cover_changed:
                    self._batch_dirty_recompute[author.id].add(merged_or_same.id)
            else:
                self.recompute(merged_or_same)
            return merged_or_same

        candidates = self._candidate_groups(author.id)
        index = self._ephemeral_index(author.id, candidates)
        return await self._assign_ungrouped(
            work,
            author,
            entry,
            index,
            allow_cover_fetch=bool(self.cover_hasher),
        )

    def assign_without_cover(self, work: Work, author: Author) -> WorkGroup:
        author_names = self._author_names_for_normalization(author)
        fingerprint, fp_changed = self._upsert_fingerprint_checked(work, author_names)
        if work.group_membership:
            group = work.group_membership.group
            if fp_changed:
                self._cache_add_group(group)
            return group
        entry = self._cached_entry(work, fingerprint)
        candidates = self._candidate_groups(author.id)
        index = self._ephemeral_index(author.id, candidates)
        return self._assign_ungrouped_sync(work, entry, index)

    async def _evaluate_title_hit(
        self,
        entry: PreparedWorkEntry,
        hit: TitleCandidateHit,
        index: AuthorCandidateIndex,
        *,
        allow_cover_fetch: bool,
    ) -> tuple[float, str, int | None, tuple[WorkGroup, float, list[str]] | None]:
        """Evaluate whether a title hit qualifies for auto-merge or produces a suggestion."""
        group = hit.group
        matched = hit.matched_entry
        if hit.match_kind == "primary_exact":
            if not extreme_page_mismatch(entry.page_count, matched.page_count):
                return 0.99, "title_exact", None, None
            return 0.0, "", None, (group, 0.78, ["核心标题一致，但页数差异较大"])

        if hit.match_kind == "alias_exact":
            if not page_count_conflict(
                entry.page_count, matched.page_count
            ) and not extreme_page_mismatch(entry.page_count, matched.page_count):
                return 0.98, "title_exact", None, None
            return 0.0, "", None, (group, 0.82, ["来源别名一致，但页数存在差异"])

        if hit.match_kind == "folded_exact":
            if (
                not page_count_conflict(entry.page_count, matched.page_count)
                and not extreme_page_mismatch(entry.page_count, matched.page_count)
                and years_match(entry.year, matched.year)
            ):
                cover_distance: int | None = None
                cover_ok = False
                if entry.cover is not None:
                    if allow_cover_fetch:
                        await self._ensure_group_covers(group)
                        index.add_group(group, self._cached_entry)
                    cover_rep, cover_distance, cover_mode = index.best_cover_for_group(
                        entry, group.id, max_distance=_TITLE_WITH_COVER_DISTANCE
                    )
                    cover_ok = (
                        cover_distance is not None
                        and cover_mode != "legacy"
                        and cover_distance <= _TITLE_WITH_COVER_DISTANCE
                        and (
                            cover_mode != "crop"
                            or page_counts_match(
                                entry.page_count,
                                (cover_rep or matched).page_count,
                            )
                        )
                    )
                pages_ok = (
                    entry.page_count is not None
                    and matched.page_count is not None
                    and page_counts_match(entry.page_count, matched.page_count)
                )
                if cover_ok or pages_ok:
                    return 0.96, "title_folded_corroborated", cover_distance, None
                return 0.0, "", cover_distance, (group, 0.88, ["繁简归一化核心标题一致"])
            return 0.0, "", None, None

        if (
            hit.match_kind == "fuzzy"
            and hit.score >= _TITLE_FUZZY_THRESHOLD
            and entry.identity_length >= 6
            and not page_count_conflict(entry.page_count, matched.page_count)
            and years_match(entry.year, matched.year)
        ):
            return hit.score, "title_fuzzy", None, None

        cover_distance = None
        if hit.score >= _TITLE_WITH_COVER_THRESHOLD and entry.identity_length >= 4:
            if entry.cover is not None:
                if allow_cover_fetch:
                    await self._ensure_group_covers(group)
                    index.add_group(group, self._cached_entry)
                cover_rep, cover_distance, cover_mode = index.best_cover_for_group(
                    entry, group.id, max_distance=_TITLE_WITH_COVER_DISTANCE
                )
                ref = cover_rep or matched
                if (
                    cover_distance is not None
                    and cover_mode != "legacy"
                    and cover_distance <= _TITLE_WITH_COVER_DISTANCE
                    and (
                        cover_mode != "crop"
                        or page_counts_match(entry.page_count, ref.page_count)
                    )
                    and not page_count_conflict(entry.page_count, ref.page_count)
                    and years_match(entry.year, ref.year)
                ):
                    confidence = min(
                        0.98,
                        0.78
                        + hit.score * 0.15
                        + (_TITLE_WITH_COVER_DISTANCE - cover_distance) * 0.006,
                    )
                    return confidence, "title_fuzzy_cover", cover_distance, None

        return 0.0, "", cover_distance, None

    def _record_cover_suggestion(
        self,
        source_group: WorkGroup,
        sug_hit: tuple[WorkGroup, PreparedWorkEntry, int, str, bool] | None,
        author_id: int | None,
    ) -> None:
        if sug_hit is None:
            return
        sug_group, _, sug_dist, _, one_sided = sug_hit
        if one_sided:
            if sug_dist <= _STRONG_COVER_DISTANCE:
                self._suggest(
                    source_group,
                    sug_group,
                    0.80,
                    [
                        f"封面视觉距离 {sug_dist}",
                        "一方缺少作品编号，疑似同一作品或合集/单篇",
                    ],
                    author_id=author_id,
                )
        else:
            self._suggest(
                source_group,
                sug_group,
                0.76,
                [f"封面视觉距离 {sug_dist}", "同一作者，标题语言或写法不同"],
                author_id=author_id,
            )

    async def _assign_ungrouped(
        self,
        work: Work,
        author: Author,
        entry: PreparedWorkEntry,
        index: AuthorCandidateIndex,
        *,
        allow_cover_fetch: bool,
    ) -> WorkGroup:
        left_primary = work_source_anchor(work)
        left_members = (left_primary,)
        hits = index.title_candidates(
            entry,
            left_primary,
            left_members,
            allow_manual=True,
            min_score=0.68,
            fuzzy_cover_threshold=_TITLE_WITH_COVER_THRESHOLD,
        )

        best_hit = hits[0] if hits else None
        cover_distance: int | None = None
        deferred_suggestion: tuple[WorkGroup, float, list[str]] | None = None
        for idx, hit in enumerate(hits):
            confidence, method, dist, sug = await self._evaluate_title_hit(
                entry, hit, index, allow_cover_fetch=allow_cover_fetch
            )
            if idx == 0:
                cover_distance = dist
                deferred_suggestion = sug
            if method:
                return self._attach(work, hit.group, confidence, method)

        auto_cover, sug_cover = index.find_cover_candidates(
            entry,
            left_primary,
            left_members,
            allow_manual_auto=True,
            strong_distance=_STRONG_COVER_DISTANCE,
            suggest_distance=_SUGGEST_COVER_DISTANCE,
        )
        if auto_cover is not None:
            cover_group, _, cov_dist, _ = auto_cover
            confidence = 0.96 - cov_dist * 0.01
            return self._attach(work, cover_group, confidence, "cover_number_author")

        new_group = self._create_group(work, entry.fingerprint, "new")
        if deferred_suggestion is not None:
            target_group, conf, reasons = deferred_suggestion
            self._suggest(new_group, target_group, conf, reasons, author_id=author.id)
            return new_group

        if best_hit is not None and best_hit.score >= 0.68:
            reasons = [f"标题相似度 {best_hit.score:.0%}"]
            if cover_distance is not None:
                reasons.append(f"封面视觉距离 {cover_distance}")
            self._suggest(
                new_group, best_hit.group, best_hit.score, reasons, author_id=author.id
            )
            return new_group

        self._record_cover_suggestion(new_group, sug_cover, author.id)
        return new_group

    def _assign_ungrouped_sync(
        self,
        work: Work,
        entry: PreparedWorkEntry,
        index: AuthorCandidateIndex,
    ) -> WorkGroup:
        left_primary = work_source_anchor(work)
        left_members = (left_primary,)
        best_hit = index.best_title_candidate(
            entry,
            left_primary,
            left_members,
            allow_manual=True,
            min_score=0.68,
        )
        deferred_suggestion: tuple[WorkGroup, float, list[str]] | None = None
        if best_hit is not None:
            group = best_hit.group
            matched = best_hit.matched_entry
            if best_hit.match_kind == "primary_exact":
                if not extreme_page_mismatch(entry.page_count, matched.page_count):
                    return self._attach(work, group, 0.99, "title_exact")
                deferred_suggestion = (group, 0.78, ["核心标题一致，但页数差异较大"])
            elif best_hit.match_kind == "alias_exact":
                if not page_count_conflict(
                    entry.page_count, matched.page_count
                ) and not extreme_page_mismatch(entry.page_count, matched.page_count):
                    return self._attach(work, group, 0.98, "title_exact")
                deferred_suggestion = (group, 0.82, ["来源别名一致，但页数存在差异"])
            elif best_hit.match_kind == "folded_exact":
                if (
                    entry.page_count is not None
                    and matched.page_count is not None
                    and page_counts_match(entry.page_count, matched.page_count)
                    and years_match(entry.year, matched.year)
                ):
                    return self._attach(work, group, 0.96, "title_folded_corroborated")
                deferred_suggestion = (group, 0.88, ["繁简归一化核心标题一致"])
            elif (
                best_hit.match_kind == "fuzzy"
                and best_hit.score >= _TITLE_FUZZY_THRESHOLD
                and entry.identity_length >= 6
                and not page_count_conflict(entry.page_count, matched.page_count)
                and years_match(entry.year, matched.year)
            ):
                return self._attach(work, group, best_hit.score, "title_fuzzy")

        new_group = self._create_group(work, entry.fingerprint, "backfill")
        if deferred_suggestion is not None:
            target_group, conf, reasons = deferred_suggestion
            self._suggest(new_group, target_group, conf, reasons)
        elif best_hit is not None and best_hit.score >= 0.68:
            self._suggest(
                new_group,
                best_hit.group,
                best_hit.score,
                [f"标题相似度 {best_hit.score:.0%}"],
            )
        return new_group

    async def _ensure_group_covers(self, group: WorkGroup) -> None:
        if not self.cover_hasher:
            return
        for member in group.members:
            fp = member.work.fingerprint
            if fp is not None and not is_current_fingerprint(fp.cover_fingerprint):
                changed = await self._ensure_cover_fingerprint(fp, member.work.cover_url)
                if changed:
                    self._invalidate_fp_cache(member.work_id)

    def merge_groups(
        self,
        target: WorkGroup,
        source: WorkGroup,
        *,
        manual: bool = True,
        method: str = "manual",
    ) -> WorkGroup:
        if target.id == source.id:
            return target
        affected_authors = authors_for_groups(self.session, [target.id, source.id])
        target_overrides = dict(target.manual_metadata or {})
        source_overrides = dict(source.manual_metadata or {})
        conflicts = [
            key
            for key, value in source_overrides.items()
            if key in target_overrides and target_overrides[key] != value
        ]
        if conflicts:
            if not manual:
                return source
            raise ValueError("两部作品存在不同的手工设置，请先解除冲突后合并")
        target.manual_metadata = {**source_overrides, **target_overrides}
        for member in list(source.members):
            source.members.remove(member)
            member.group = target
            member.group_id = target.id
            member.is_manual = manual
            member.match_method = method
            member.confidence = 1.0 if manual else max(member.confidence, 0.91)
        self.session.execute(
            delete(MergeSuggestion).where(
                or_(
                    MergeSuggestion.source_group_id.in_([target.id, source.id]),
                    MergeSuggestion.target_group_id.in_([target.id, source.id]),
                )
            )
        )
        for sug_map in self._batch_suggestions.values():
            for key in [
                k for k in sug_map if target.id in k or source.id in k
            ]:
                sug_map.pop(key, None)
        self.session.flush()
        self._cache_remove_group(source.id)
        self.session.delete(source)
        self.recompute(target)
        self._cache_add_group(target)
        bump_authors_for_groups(
            self.session,
            [target.id],
            reason="group_merged" if manual else "shared_group_merged",
            full_reconcile=True,
            extra_author_ids=affected_authors,
            exclude_author_id=None if manual else self._active_author_id,
        )
        return target

    def split_member(self, group: WorkGroup, work_id: int) -> WorkGroup:
        member = next((item for item in group.members if item.work_id == work_id), None)
        if member is None:
            raise LookupError("版本不属于该作品")
        if len(group.members) == 1:
            raise ValueError("该作品只有一个版本，无法拆分")
        affected_authors = authors_for_groups(self.session, [group.id]) | authors_for_works(
            self.session, [work_id]
        )
        work = member.work
        fingerprint = work.fingerprint or self._upsert_fingerprint(work, "")
        group.members.remove(member)
        self.session.delete(member)
        self.session.flush()
        new_group = self._create_group(work, fingerprint, "manual", is_manual=True)
        old_metadata = dict(group.manual_metadata or {})
        moved = {
            key: old_metadata.pop(key)
            for key in ("cover_work_id", "preferred_work_id")
            if old_metadata.get(key) == work_id
        }
        group.manual_metadata = old_metadata
        new_group.manual_metadata = moved
        self.recompute(group)
        self.recompute(new_group)
        bump_authors_for_groups(
            self.session,
            [group.id, new_group.id],
            reason="group_split",
            full_reconcile=True,
            extra_author_ids=affected_authors,
            exclude_author_id=self._active_author_id,
        )
        return new_group

    def recompute(self, group: WorkGroup) -> None:
        works = [member.work for member in group.members]
        if not works:
            return
        overrides = dict(group.manual_metadata or {})
        valid_ids = {work.id for work in works}
        for key in ("cover_work_id", "preferred_work_id"):
            if key in overrides and overrides[key] not in valid_ids:
                overrides.pop(key)
        group.manual_metadata = overrides
        preferred = next(
            (work for work in works if work.id == overrides.get("preferred_work_id")), None
        )
        preferred_works = sorted(
            works, key=lambda work: (work is not preferred, work.cover_url is None)
        )
        fingerprints = [work.fingerprint for work in works if work.fingerprint]
        title_candidates = [item.normalized_title for item in fingerprints if item.normalized_title]
        if preferred:
            group.title = (
                preferred.fingerprint.normalized_title
                if preferred.fingerprint and preferred.fingerprint.normalized_title
                else preferred.title
            )
        elif title_candidates:
            group.title = min(title_candidates, key=lambda value: (len(value), value))
        else:
            group.title = min((work.title for work in works), key=len)
        descriptions = [work.description for work in works if work.description]
        group.description = (
            preferred.description
            if preferred and preferred.description
            else max(descriptions, key=len, default=None)
        )
        cover = next((work for work in works if work.id == overrides.get("cover_work_id")), None)
        group.cover_url = (
            cover.cover_url
            if cover and cover.cover_url
            else next((work.cover_url for work in preferred_works if work.cover_url), None)
        )
        group.status = next((work.status for work in preferred_works if work.status), None)
        group.year = next((work.year for work in preferred_works if work.year), None)
        group.language = next((work.language for work in preferred_works if work.language), None)
        group.tags = sorted({tag for work in works for tag in (work.tags or [])})
        for field in ("title", "description", "year", "status", "language", "tags"):
            if field in overrides:
                setattr(group, field, overrides[field])
        dates = [
            as_utc(source.source_updated_at)
            for work in works
            for source in work.sources
            if source.source_updated_at
        ]
        group.first_source_at = min(dates, default=None)
        group.latest_source_at = max(dates, default=None)
        group.updated_at = datetime.now(UTC)

    def _upsert_fingerprint(
        self, work: Work, author_name: str | Iterable[str]
    ) -> WorkFingerprint:
        fingerprint, _ = self._upsert_fingerprint_checked(work, author_name)
        return fingerprint

    def _upsert_fingerprint_checked(
        self, work: Work, author_name: str | Iterable[str]
    ) -> tuple[WorkFingerprint, bool]:
        normalized = normalize_title(work.title, author_name) or work.title.casefold()
        primary_core = parse_title_identity(work.title, normalized).identity_core
        aliases: set[str] = set()
        for source in work.sources:
            for raw_alias in self._strings((source.raw_metadata or {}).get("altTitles", [])):
                norm_alias = normalize_title(raw_alias, author_name)
                if not norm_alias:
                    continue
                core_alias = parse_title_identity(raw_alias, norm_alias).identity_core
                if core_alias and core_alias != primary_core and core_alias != normalized:
                    aliases.add(core_alias)
        sorted_aliases = sorted(aliases)
        labels = extract_variant_labels(work.title, work.language)
        page_counts = [
            source.raw_metadata.get("page_count")
            for source in work.sources
            if isinstance((source.raw_metadata or {}).get("page_count"), int)
        ]
        page_count = max(page_counts, default=None)
        fingerprint = work.fingerprint
        if fingerprint is None:
            fingerprint = WorkFingerprint(
                work=work,
                normalized_title=normalized,
                title_aliases=sorted_aliases,
                variant_labels=labels,
                page_count=page_count,
            )
            self.session.add(fingerprint)
            self.session.flush()
            self._invalidate_fp_cache(work.id)
            return fingerprint, True

        changed = (
            fingerprint.normalized_title != normalized
            or list(fingerprint.title_aliases or []) != sorted_aliases
            or list(fingerprint.variant_labels or []) != labels
            or fingerprint.page_count != page_count
        )
        if changed:
            fingerprint.normalized_title = normalized
            fingerprint.title_aliases = sorted_aliases
            fingerprint.variant_labels = labels
            fingerprint.page_count = page_count
            self.session.flush()
            self._invalidate_fp_cache(work.id)
        return fingerprint, changed

    async def _ensure_cover_fingerprint(
        self, fingerprint: WorkFingerprint, cover_url: str | None
    ) -> bool:
        if not self.cover_hasher or not cover_url:
            return False
        current = fingerprint.cover_fingerprint
        if is_current_fingerprint(current) and current.get("source_url") == cover_url:
            return False
        before_hash = fingerprint.cover_hash
        source = next(iter(fingerprint.work.sources), None)
        computed = await self.cover_hasher.fingerprint_url(
            cover_url,
            source.provider if source else None,
            source.external_id if source else None,
        )
        if computed is None:
            if not current or current.get("source_url") != cover_url:
                fingerprint.cover_hash = None
                fingerprint.cover_fingerprint = None
                if current is not None or before_hash is not None:
                    bump_authors_for_works(
                        self.session,
                        [fingerprint.work_id],
                        reason="cover_fingerprint_cleared",
                        full_reconcile=True,
                        exclude_author_id=self._active_author_id,
                    )
                    return True
            return False
        computed["source_url"] = cover_url
        fingerprint.cover_fingerprint = computed
        variants = computed.get("variants", [])
        fingerprint.cover_hash = variants[0].get("dhash") if variants else None
        if current != computed or before_hash != fingerprint.cover_hash:
            bump_authors_for_works(
                self.session,
                [fingerprint.work_id],
                reason="cover_fingerprint_updated",
                full_reconcile=True,
                exclude_author_id=self._active_author_id,
            )
            return True
        return False

    def _candidate_groups(self, author_id: int) -> list[WorkGroup]:
        if author_id in self._batch_groups:
            return list(self._batch_groups[author_id].values())
        record_metric("candidate_loads")
        return list(
            self.session.scalars(
                select(WorkGroup)
                .join(WorkGroupMember)
                .join(AuthorWork, AuthorWork.work_id == WorkGroupMember.work_id)
                .where(AuthorWork.author_id == author_id)
                .distinct()
            )
        )

    def _best_title_candidate(
        self,
        fingerprint: WorkFingerprint,
        groups: list[WorkGroup],
        *,
        min_score: float = 0.0,
    ) -> tuple[WorkGroup | None, float, bool]:
        index = AuthorCandidateIndex()
        for group in groups:
            index.add_group(group, self._cached_entry)
        entry = self._cached_entry(fingerprint.work, fingerprint)
        hit = index.best_title_candidate(
            entry,
            work_source_anchor(fingerprint.work),
            (work_source_anchor(fingerprint.work),),
            min_score=min_score,
        )
        if hit is None:
            return None, 0.0, False
        is_exact = hit.match_kind in {"primary_exact", "alias_exact"}
        return hit.group, hit.score, is_exact

    def _best_fingerprint(
        self, fingerprint: WorkFingerprint, group: WorkGroup
    ) -> WorkFingerprint | None:
        names = self._cached_primary_fingerprint_names(fingerprint)
        candidates = [member.work.fingerprint for member in group.members]
        candidates = [candidate for candidate in candidates if candidate]
        return max(
            candidates,
            key=lambda candidate: best_identity_similarity(
                names, self._cached_primary_fingerprint_names(candidate)
            ),
            default=None,
        )

    def _best_cover_candidate(
        self,
        fingerprint: WorkFingerprint,
        groups: list[WorkGroup],
        *,
        max_distance: int | None = None,
    ) -> tuple[WorkGroup | None, WorkFingerprint | None, int | None, str | None]:
        index = AuthorCandidateIndex()
        for group in groups:
            index.add_group(group, self._cached_entry)
        entry = self._cached_entry(fingerprint.work, fingerprint)
        group, rep, distance, mode, _ = index.best_cover_candidate(
            entry,
            work_source_anchor(fingerprint.work),
            (work_source_anchor(fingerprint.work),),
            max_distance=max_distance if max_distance is not None else _SUGGEST_COVER_DISTANCE,
            require_year_page=False,
            allow_one_sided_number=False,
        )
        return group, (rep.fingerprint if rep else None), distance, mode

    def _cached_numbering_signature(self, fingerprint: WorkFingerprint) -> tuple[int, ...]:
        work_id = fingerprint.work_id
        if work_id is not None and work_id in self._fp_num_sig_cache:
            return self._fp_num_sig_cache[work_id]
        sig = identity_number_signature(fingerprint.normalized_title)
        if work_id is not None and self._batch_groups:
            self._fp_num_sig_cache[work_id] = sig
        return sig

    def _numbering_pair_matches(self, left: WorkFingerprint, right: WorkFingerprint) -> bool:
        return self._cached_numbering_signature(left) == self._cached_numbering_signature(right)

    @staticmethod
    def _numbering_matches(left: WorkFingerprint, right: WorkFingerprint) -> bool:
        return identity_number_signature(left.normalized_title) == identity_number_signature(
            right.normalized_title
        )

    def _group_numbering_matches(
        self,
        fingerprint: WorkFingerprint,
        group: WorkGroup,
        *,
        excluding_work_id: int | None = None,
    ) -> bool:
        return all(
            member.work.fingerprint is None
            or self._numbering_pair_matches(fingerprint, member.work.fingerprint)
            for member in group.members
            if member.work_id != excluding_work_id
        )

    def _group_consistency_matches(
        self,
        entry: PreparedWorkEntry,
        group: WorkGroup,
        *,
        excluding_work_id: int | None = None,
        constrained_keys: set[str] | None = None,
    ) -> bool:
        left_anchor = work_source_anchor(entry.fingerprint.work)
        for member in group.members:
            if member.work_id == excluding_work_id:
                continue
            other_fp = member.work.fingerprint
            if other_fp is None:
                continue
            other_entry = self._cached_entry(member.work, other_fp)
            if entry.number_signature != other_entry.number_signature:
                return False
            if entry.semantics.conflicts_with(other_entry.semantics):
                return False
            if extreme_page_mismatch(entry.page_count, other_entry.page_count):
                return False
            if constrained_keys:
                right_anchor = work_source_anchor(member.work)
                if is_anchor_pair_constrained(
                    left_anchor,
                    (left_anchor,),
                    right_anchor,
                    (right_anchor,),
                    constrained_keys,
                ):
                    return False
        return True

    def _detach_automatic_membership(self, membership: WorkGroupMember) -> None:
        group = membership.group
        group.members.remove(membership)
        self.session.delete(membership)
        self.session.flush()
        if group.members:
            self.recompute(group)
            self._cache_add_group(group)
        else:
            self._cache_remove_group(group.id)
            self.session.delete(group)
            self.session.flush()

    async def _reconcile_existing(
        self,
        work: Work,
        author: Author,
        entry: PreparedWorkEntry,
        current_group: WorkGroup,
        *,
        allow_cover_fetch: bool,
    ) -> WorkGroup:
        candidates = [
            group for group in self._candidate_groups(author.id) if group.id != current_group.id
        ]
        if not candidates:
            return current_group
        index = self._ephemeral_index(author.id, candidates)
        skip_group_ids: set[int] | None = None
        if (
            self._batch_defer_recompute
            and self._active_author_id == author.id
            and author.id in self._batch_done_seq
        ):
            cur_mod = self._batch_mod_seq[author.id].get(current_group.id, 0)
            skip_group_ids = {
                gid
                for gid, d_seq in self._batch_done_seq[author.id].items()
                if cur_mod < d_seq
            }
        left_primary, left_members = group_anchor_info(current_group)
        hits = index.title_candidates(
            entry,
            left_primary,
            left_members,
            excluding_group_id=current_group.id,
            skip_group_ids=skip_group_ids,
            allow_manual=False,
            min_score=0.72,
            fuzzy_cover_threshold=_TITLE_WITH_COVER_THRESHOLD,
        )
        for hit in hits:
            _, method, _, _ = await self._evaluate_title_hit(
                entry, hit, index, allow_cover_fetch=allow_cover_fetch
            )
            if method:
                return self.merge_groups(
                    hit.group,
                    current_group,
                    manual=False,
                    method=f"{method}_reconcile",
                )

        return self._reconcile_by_cover(
            author, entry, current_group, index, skip_group_ids=skip_group_ids
        )

    def _reconcile_by_cover(
        self,
        author: Author,
        entry: PreparedWorkEntry,
        current_group: WorkGroup,
        index: AuthorCandidateIndex,
        *,
        skip_group_ids: set[int] | None = None,
    ) -> WorkGroup:
        if entry.cover is None:
            return current_group
        left_primary, left_members = group_anchor_info(current_group)
        auto_cover, sug_cover = index.find_cover_candidates(
            entry,
            left_primary,
            left_members,
            excluding_group_id=current_group.id,
            skip_group_ids=skip_group_ids,
            allow_manual_auto=False,
            strong_distance=_STRONG_COVER_DISTANCE,
            suggest_distance=_SUGGEST_COVER_DISTANCE,
        )
        if auto_cover is not None:
            target, _, _, _ = auto_cover
            return self.merge_groups(
                target,
                current_group,
                manual=False,
                method="cover_number_author_reconcile",
            )
        self._record_cover_suggestion(current_group, sug_cover, author.id)
        return current_group

    def _create_group(
        self,
        work: Work,
        fingerprint: WorkFingerprint,
        method: str,
        is_manual: bool = False,
    ) -> WorkGroup:
        group = WorkGroup(
            title=fingerprint.normalized_title or work.title,
            description=work.description,
            cover_url=work.cover_url,
            status=work.status,
            year=work.year,
            language=work.language,
            tags=work.tags or [],
        )
        group.members.append(
            WorkGroupMember(
                work=work,
                confidence=1.0,
                match_method=method,
                is_manual=is_manual,
            )
        )
        self.session.add(group)
        self.session.flush()
        self.recompute(group)
        self._cache_add_group(group)
        return group

    def _attach(self, work: Work, group: WorkGroup, confidence: float, method: str) -> WorkGroup:
        group.members.append(
            WorkGroupMember(
                work=work,
                confidence=confidence,
                match_method=method,
            )
        )
        self.session.flush()
        self.recompute(group)
        self._cache_add_group(group)
        return group

    def _suggest(
        self,
        source: WorkGroup,
        target: WorkGroup,
        confidence: float,
        reasons: list[str],
        *,
        author_id: int | None = None,
    ) -> None:
        if source.id is None or target.id is None or source.id == target.id:
            return
        constrained_keys = (
            self._batch_constrained_keys.get(author_id)
            if author_id is not None and author_id in self._batch_constrained_keys
            else None
        )
        left_primary, left_members = group_anchor_info(source)
        right_primary, right_members = group_anchor_info(target)
        if constrained_keys is not None:
            if is_anchor_pair_constrained(
                left_primary, left_members, right_primary, right_members, constrained_keys
            ):
                return
        else:
            is_constrained = self.session.scalar(
                select(PairConstraint.id).where(
                    PairConstraint.candidate_key == candidate_key(source, target),
                    PairConstraint.decision == "different_work",
                )
            )
            if is_constrained is not None:
                return

        pair_key = (min(source.id, target.id), max(source.id, target.id))
        sug_map = (
            self._batch_suggestions.get(author_id)
            if author_id is not None and author_id in self._batch_suggestions
            else None
        )
        existing = sug_map.get(pair_key) if sug_map is not None else None
        if existing is None and sug_map is None:
            existing = self.session.scalar(
                select(MergeSuggestion).where(
                    or_(
                        MergeSuggestion.source_group_id == source.id,
                        MergeSuggestion.target_group_id == source.id,
                    ),
                    or_(
                        MergeSuggestion.source_group_id == target.id,
                        MergeSuggestion.target_group_id == target.id,
                    ),
                )
            )
        if existing is None:
            suggestion = MergeSuggestion(
                source_group_id=source.id,
                target_group_id=target.id,
                confidence=confidence,
                reasons=reasons,
            )
            self.session.add(suggestion)
            if sug_map is not None:
                sug_map[pair_key] = suggestion
        else:
            existing.confidence = confidence
            existing.reasons = reasons

    @staticmethod
    def _suggestion_still_valid(
        suggestion: MergeSuggestion, source: WorkGroup, target: WorkGroup
    ) -> bool:
        from app.modules.agent_review.candidates import build_candidate_evidence

        left_nums = {
            identity_number_signature(m.work.fingerprint.normalized_title)
            for m in source.members
            if m.work.fingerprint
        }
        right_nums = {
            identity_number_signature(m.work.fingerprint.normalized_title)
            for m in target.members
            if m.work.fingerprint
        }
        if left_nums and right_nums:
            has_shared_num = bool(left_nums & right_nums)
            if not has_shared_num:
                one_sided = (
                    all(not n for n in left_nums) and all(bool(n) for n in right_nums)
                ) or (
                    all(bool(n) for n in left_nums) and all(not n for n in right_nums)
                )
                if not one_sided:
                    return False

        left_sems = {extract_title_semantics(m.work.title) for m in source.members}
        right_sems = {extract_title_semantics(m.work.title) for m in target.members}
        if any(ls.conflicts_with(rs) for ls in left_sems for rs in right_sems):
            return False

        evidence = build_candidate_evidence(suggestion, source, target)
        title_support = bool(
            set(evidence.available_evidence)
            & {"core_title_match", "source_alias_match", "core_title_similarity"}
        )
        visual_support = bool(
            set(evidence.available_evidence)
            & {"cover_hash_strong", "cover_hash_weak", "cover_crop_match"}
        )
        return title_support or visual_support

    def prune_pending_suggestions(self, author_id: int) -> int:
        """Drop stale fuzzy candidates that no longer pass the retrieval policy."""
        group_ids = set(
            self.session.scalars(
                select(WorkGroupMember.group_id)
                .join(AuthorWork, AuthorWork.work_id == WorkGroupMember.work_id)
                .where(AuthorWork.author_id == author_id)
            )
        )
        if not group_ids:
            return 0
        group_id_list = sorted(group_ids)
        suggestions: list[MergeSuggestion] = []
        seen_ids: set[int] = set()
        for offset in range(0, len(group_id_list), 400):
            chunk = group_id_list[offset : offset + 400]
            rows = self.session.scalars(
                select(MergeSuggestion).where(
                    MergeSuggestion.status == "pending",
                    MergeSuggestion.source_group_id.in_(chunk),
                    MergeSuggestion.target_group_id.in_(group_id_list),
                )
            )
            for row in rows:
                if row.id not in seen_ids:
                    seen_ids.add(row.id)
                    suggestions.append(row)
        if not suggestions:
            return 0
        needed_ids = {
            gid
            for item in suggestions
            for gid in (item.source_group_id, item.target_group_id)
        }
        groups_by_id = WorkGroupRepository(self.session).get_many(needed_ids)
        removed = 0
        for suggestion in suggestions:
            source = groups_by_id.get(suggestion.source_group_id)
            target = groups_by_id.get(suggestion.target_group_id)
            if source is None or target is None:
                continue
            if not self._suggestion_still_valid(suggestion, source, target):
                self.session.delete(suggestion)
                removed += 1
        self.session.flush()
        return removed

    def apply_prefetched_covers(
        self,
        works: Sequence[Work],
        prefetched: dict[tuple[str, str | None, str | None], dict | None],
        *,
        author_id: int | None = None,
    ) -> int:
        """Persist prefetched cover hashes for existing works before skip checks."""
        if not prefetched or not any(val is not None for val in prefetched.values()):
            return 0
        candidates = list(works)
        seen_work_ids = {work.id for work in candidates}
        if author_id is not None:
            prefetched_urls = {url for (url, _, _), val in prefetched.items() if val is not None}
            if prefetched_urls:
                extra_works = list(
                    self.session.scalars(
                        select(Work)
                        .join(AuthorWork, AuthorWork.work_id == Work.id)
                        .where(
                            AuthorWork.author_id == author_id,
                            Work.cover_url.in_(prefetched_urls),
                        )
                    ).unique()
                )
                for extra in extra_works:
                    if extra.id not in seen_work_ids:
                        candidates.append(extra)
                        seen_work_ids.add(extra.id)

        updated_ids: list[int] = []
        for work in candidates:
            if not work.cover_url or work.fingerprint is None:
                continue
            current = work.fingerprint.cover_fingerprint
            if is_current_fingerprint(current) and current.get("source_url") == work.cover_url:
                continue
            source = next(
                (
                    src
                    for src in work.sources
                    if prefetched.get((work.cover_url, src.provider, src.external_id))
                    is not None
                ),
                None,
            )
            if source is None:
                continue
            key = (work.cover_url, source.provider, source.external_id)
            computed = prefetched.get(key)
            if computed is None:
                continue
            payload = dict(computed)
            payload["source_url"] = work.cover_url
            variants = payload.get("variants", [])
            next_hash = variants[0].get("dhash") if variants else None
            if (
                current != payload
                or work.fingerprint.cover_hash != next_hash
            ):
                work.fingerprint.cover_fingerprint = payload
                work.fingerprint.cover_hash = next_hash
                self._invalidate_fp_cache(work.id)
                updated_ids.append(work.id)
        if updated_ids:
            self.session.flush()
            bump_authors_for_works(
                self.session,
                updated_ids,
                reason="cover_fingerprint_updated",
                full_reconcile=True,
                exclude_author_id=self._active_author_id,
            )
        return len(updated_ids)

    async def sync_author_after_writes(
        self,
        author: Author,
        *,
        touched_works: Sequence[Work],
        prefetched_covers: dict[tuple[str, str | None, str | None], dict | None] | None = None,
        force: bool = False,
    ) -> bool:
        """Decide whether aggregation is needed after writing works; run batch or skip."""
        from app.modules.catalog.invalidation import (
            needs_author_aggregation,
            should_full_reconcile,
        )

        if prefetched_covers:
            self.apply_prefetched_covers(
                touched_works, prefetched_covers, author_id=author.id
            )
        self.session.flush()
        if not needs_author_aggregation(self.session, author.id, force=force):
            record_metric("aggregation_skipped")
            return False

        snapshot_revision = capture_author_revision(self.session, author.id)
        full_reconcile = should_full_reconcile(self.session, author.id, force=force)
        await self.reconcile_author(author, works=None if full_reconcile else touched_works)
        complete_author_revision(self.session, author.id, snapshot_revision)
        return True

    def _cached_primary_fingerprint_names(self, fingerprint: WorkFingerprint) -> set[str]:
        work_id = fingerprint.work_id
        if work_id is not None and work_id in self._fp_primary_names_cache:
            return self._fp_primary_names_cache[work_id]
        names = self._primary_fingerprint_names(fingerprint)
        if work_id is not None and self._batch_groups:
            self._fp_primary_names_cache[work_id] = names
        return names

    @staticmethod
    def _fingerprint_names(fingerprint: WorkFingerprint) -> set[str]:
        return {
            _canonicalize_numbers(value)
            for value in identity_names(
                fingerprint.work.title,
                fingerprint.normalized_title,
                fingerprint.title_aliases,
            )
        }

    @staticmethod
    def _primary_fingerprint_names(fingerprint: WorkFingerprint) -> set[str]:
        return {
            _canonicalize_numbers(value)
            for value in identity_names(
                fingerprint.work.title,
                fingerprint.normalized_title,
                [],
            )
        }

    @classmethod
    def _identity_length(cls, fingerprint: WorkFingerprint) -> int:
        return len(_compact(fingerprint.normalized_title))

    @staticmethod
    def _page_counts_match(left: WorkFingerprint, right: WorkFingerprint) -> bool:
        return page_counts_match(left.page_count, right.page_count)

    @classmethod
    def _page_counts_compatible(cls, left: WorkFingerprint, right: WorkFingerprint) -> bool:
        return page_counts_compatible(left.page_count, right.page_count)

    @staticmethod
    def _years_match(left: Work, right: Work) -> bool:
        return years_match(left.year, right.year)

    @classmethod
    def _strings(cls, value: Any) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return [item for entry in value for item in cls._strings(entry)]
        if isinstance(value, dict):
            return [item for entry in value.values() for item in cls._strings(entry)]
        return []


def backfill_work_groups(session: Session) -> int:
    service = AggregationService(session)
    created = 0
    rows = session.execute(
        select(Work, Author)
        .join(AuthorWork, AuthorWork.work_id == Work.id)
        .join(Author, Author.id == AuthorWork.author_id)
        .order_by(Author.id, Work.id)
    ).all()
    current_author_id: int | None = None
    try:
        for work, author in rows:
            if author.id != current_author_id:
                if current_author_id is not None:
                    service.end_author_batch(current_author_id)
                current_author_id = author.id
                service.begin_author_batch(author.id)
                service._active_author_id = author.id
            if work.group_membership is None:
                service.assign_without_cover(work, author)
                created += 1
    finally:
        service._active_author_id = None
        if current_author_id is not None:
            service.end_author_batch(current_author_id)
    session.commit()
    return created


def prune_invalid_suggestions(session: Session) -> int:
    suggestions = list(
        session.scalars(select(MergeSuggestion).where(MergeSuggestion.status == "pending"))
    )
    if not suggestions:
        return 0
    group_ids = {
        gid
        for item in suggestions
        for gid in (item.source_group_id, item.target_group_id)
    }
    groups_by_id = WorkGroupRepository(session).get_many(group_ids)
    removed = 0
    for suggestion in suggestions:
        source = groups_by_id.get(suggestion.source_group_id)
        target = groups_by_id.get(suggestion.target_group_id)
        if source is None or target is None:
            continue
        if not AggregationService._suggestion_still_valid(suggestion, source, target):
            session.delete(suggestion)
            removed += 1
    if removed:
        session.commit()
    return removed

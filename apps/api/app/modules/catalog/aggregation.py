import html
import io
import re
import unicodedata
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx
from PIL import Image, UnidentifiedImageError
from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

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
from app.modules.catalog.cover_fingerprint import (
    compare_fingerprints,
    fingerprint_image,
    hash_distance,
    is_current_fingerprint,
)
from app.modules.catalog.pair_identity import candidate_key
from app.modules.catalog.title_identity import (
    best_identity_similarity,
    identity_names,
)
from app.providers.errors import ProviderError

if TYPE_CHECKING:
    from app.providers.registry import ProviderRegistry

_BRACKET_RE = re.compile(r"([\[【（(])([^\]】）)]{1,100})[\]】）)]")
_VERSION_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"汉化|漢化|翻译|翻譯|翻訳", "汉化/翻译"),
    (r"上色|全彩|カラー|full[ -]?colou?r", "上色/全彩"),
    (
        r"无码|無碼|无修正|無修正|去码|去碼|uncensored|decensored",
        "无码/无修正",
    ),
    (r"ai(?:去码|去碼|(?:无|無)?修正|uncensored)", "AI 处理"),
    (r"\bdl\s*版|digital", "DL 版"),
    (r"扫图|掃圖|scan", "扫图版"),
    (r"\boriginal\b|原版|オリジナル", "原版"),
    (r"\bincomplete\b|未完成", "不完整版"),
    (r"v\s*\d+\b", "修订版"),
)
_STATUS_NOISE_RE = re.compile(
    r"完[结結]|连载|連載|ongoing|completed|incomplete", re.IGNORECASE
)
_EVENT_PREFIX_RE = re.compile(
    r"^\s*[（(](?:"
    r"c\d+[a-z]?|comiket\s*\d+|comic\s*market\s*\d+|"
    r"ac\d+|エアコミケ\s*\d+|gw超同人祭|"
    r"comitia\s*\d+|コミティア\s*\d+"
    r")[）)]\s*",
    re.IGNORECASE,
)
_CHAPTER_SUFFIX_RE = re.compile(
    r"\s+(?:第\s*)?\d+(?:\.\d+)?\s*(?:話|话|화|回|ch(?:apter)?\.?)\b.*$",
    re.IGNORECASE,
)
_TITLE_FUZZY_THRESHOLD = 0.94
_TITLE_WITH_COVER_THRESHOLD = 0.72
_TITLE_WITH_COVER_DISTANCE = 10
_STRONG_COVER_DISTANCE = 8
_SUGGEST_COVER_DISTANCE = 13


def extract_variant_labels(title: str, language: str | None = None) -> list[str]:
    value = unicodedata.normalize("NFKC", html.unescape(title))
    labels: list[str] = []
    for pattern, label in _VERSION_PATTERNS:
        if re.search(pattern, value, re.IGNORECASE) and label not in labels:
            labels.append(label)
    for match in _BRACKET_RE.finditer(value):
        content = " ".join(match.group(2).split())
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


def normalize_title(title: str, author_name: str = "") -> str:
    value = unicodedata.normalize("NFKC", html.unescape(title)).casefold()
    value = re.sub(r"[×✕✖]", " x ", value)
    value = _EVENT_PREFIX_RE.sub("", value)
    normalized_author = _compact(author_name)

    def replace_bracket(match: re.Match[str]) -> str:
        content = match.group(2)
        compact_content = _compact(content)
        is_leading = match.start() <= 1
        is_version = any(
            re.search(pattern, content, re.IGNORECASE)
            for pattern, _ in _VERSION_PATTERNS
        ) or bool(_STATUS_NOISE_RE.search(content))
        is_creator = is_leading and bool(
            (normalized_author and normalized_author in compact_content)
            or re.search(r"works|circle|サークル", content, re.IGNORECASE)
            or "(" in content
        )
        return " " if is_version or is_creator else f" {content} "

    value = _BRACKET_RE.sub(replace_bracket, value)
    value = _CHAPTER_SUFFIX_RE.sub("", value)
    value = re.sub(r"\s+\d+\s*(?:話|话|화|回)\s*$", "", value, flags=re.IGNORECASE)
    value = _STATUS_NOISE_RE.sub(" ", value)
    for pattern, _ in _VERSION_PATTERNS:
        value = re.sub(pattern, " ", value, flags=re.IGNORECASE)
    if normalized_author:
        value = re.sub(re.escape(author_name.casefold()), " ", value, flags=re.IGNORECASE)
    value = re.sub(r"[^\w\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]+", " ", value)
    return " ".join(value.split()).strip()


def _compact(value: str) -> str:
    return re.sub(r"\W+", "", unicodedata.normalize("NFKC", value).casefold())


def identity_number_signature(value: str) -> tuple[int, ...]:
    """Extract work numbers while ignoring common year and resolution noise."""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = re.sub(r"\b(?:19|20)\d{2}\b", " ", normalized)
    normalized = re.sub(r"\b\d{3,4}\s*[pk]\b", " ", normalized)
    numbers = (int(number) for number in re.findall(r"\d+", normalized))
    return tuple(dict.fromkeys(numbers))


def _canonicalize_numbers(value: str) -> str:
    return re.sub(r"\d+", lambda match: str(int(match.group())), value)


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


class CoverHasher:
    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        providers: "ProviderRegistry | None" = None,
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
            with Image.open(io.BytesIO(content)) as image:
                return fingerprint_image(image)
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

    async def assign(self, work: Work, author: Author) -> WorkGroup:
        fingerprint = self._upsert_fingerprint(work, author.name)
        if self.cover_hasher:
            await self._ensure_cover_fingerprint(fingerprint, work.cover_url)
        membership = work.group_membership
        if (
            membership
            and not any(member.is_manual for member in membership.group.members)
            and not self._group_numbering_matches(
                fingerprint, membership.group, excluding_work_id=work.id
            )
        ):
            self._detach_automatic_membership(membership)
            membership = None

        if membership:
            group = membership.group
            if not any(member.is_manual for member in group.members):
                group = self._reconcile_existing(
                    work, author, fingerprint, group
                )
            self.recompute(group)
            return group

        candidates = self._candidate_groups(author.id)
        best_group, title_score, exact = self._best_title_candidate(fingerprint, candidates)
        cover_distance: int | None = None

        if best_group and not exact and title_score >= 0.58 and self.cover_hasher:
            candidate_fingerprint = self._best_fingerprint(fingerprint, best_group)
            if candidate_fingerprint and not is_current_fingerprint(
                candidate_fingerprint.cover_fingerprint
            ):
                await self._ensure_cover_fingerprint(
                    candidate_fingerprint, candidate_fingerprint.work.cover_url
                )
            if candidate_fingerprint:
                cover_distance, _, _ = cover_fingerprint_distance(
                    fingerprint, candidate_fingerprint
                )

        if best_group and exact:
            return self._attach(work, best_group, 0.99, "title_exact")
        if (
            best_group
            and title_score >= _TITLE_FUZZY_THRESHOLD
            and self._identity_length(fingerprint) >= 6
        ):
            return self._attach(work, best_group, title_score, "title_fuzzy")
        if (
            best_group
            and title_score >= _TITLE_WITH_COVER_THRESHOLD
            and cover_distance is not None
            and cover_distance <= _TITLE_WITH_COVER_DISTANCE
        ):
            confidence = min(
                0.98,
                0.78
                + title_score * 0.15
                + (_TITLE_WITH_COVER_DISTANCE - cover_distance) * 0.006,
            )
            return self._attach(work, best_group, confidence, "title_fuzzy_cover")

        (
            cover_group,
            cover_fingerprint,
            cover_distance,
            cover_mode,
        ) = self._best_cover_candidate(fingerprint, candidates)
        if (
            cover_group
            and cover_fingerprint
            and cover_distance is not None
            and cover_distance <= _STRONG_COVER_DISTANCE
            and self._page_counts_compatible(fingerprint, cover_fingerprint)
            and (
                cover_mode != "crop"
                or self._page_counts_match(fingerprint, cover_fingerprint)
            )
            and self._years_match(work, cover_fingerprint.work)
        ):
            confidence = 0.96 - cover_distance * 0.01
            return self._attach(
                work, cover_group, confidence, "cover_number_author"
            )

        group = self._create_group(work, fingerprint, "new")
        if best_group and title_score >= 0.68:
            reasons = [f"标题相似度 {title_score:.0%}"]
            if cover_distance is not None:
                reasons.append(f"封面视觉距离 {cover_distance}")
            self._suggest(group, best_group, title_score, reasons)
        return group

    def assign_without_cover(self, work: Work, author: Author) -> WorkGroup:
        fingerprint = self._upsert_fingerprint(work, author.name)
        if work.group_membership:
            return work.group_membership.group
        candidates = self._candidate_groups(author.id)
        best_group, title_score, exact = self._best_title_candidate(fingerprint, candidates)
        if best_group and exact:
            return self._attach(work, best_group, 0.99, "title_exact")
        if (
            best_group
            and title_score >= _TITLE_FUZZY_THRESHOLD
            and self._identity_length(fingerprint) >= 6
        ):
            return self._attach(work, best_group, title_score, "title_fuzzy")
        group = self._create_group(work, fingerprint, "backfill")
        if best_group and title_score >= 0.68:
            self._suggest(group, best_group, title_score, [f"标题相似度 {title_score:.0%}"])
        return group

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
        self.session.flush()
        self.session.delete(source)
        self.recompute(target)
        return target

    def split_member(self, group: WorkGroup, work_id: int) -> WorkGroup:
        member = next((item for item in group.members if item.work_id == work_id), None)
        if member is None:
            raise LookupError("版本不属于该作品")
        if len(group.members) == 1:
            raise ValueError("该作品只有一个版本，无法拆分")
        work = member.work
        fingerprint = work.fingerprint or self._upsert_fingerprint(work, "")
        group.members.remove(member)
        self.session.delete(member)
        self.session.flush()
        new_group = self._create_group(work, fingerprint, "manual", is_manual=True)
        self.recompute(group)
        return new_group

    def recompute(self, group: WorkGroup) -> None:
        works = [member.work for member in group.members]
        if not works:
            return
        fingerprints = [work.fingerprint for work in works if work.fingerprint]
        title_candidates = [
            fingerprint.normalized_title
            for fingerprint in fingerprints
            if fingerprint.normalized_title
        ]
        if title_candidates:
            group.title = min(title_candidates, key=lambda value: (len(value), value))
        else:
            group.title = min((work.title for work in works), key=len)
        descriptions = [work.description for work in works if work.description]
        group.description = max(descriptions, key=len, default=None)
        preferred_works = sorted(
            works,
            key=lambda work: (
                not any(source.provider == "mangadex" for source in work.sources),
                work.cover_url is None,
            ),
        )
        group.cover_url = next(
            (work.cover_url for work in preferred_works if work.cover_url), None
        )
        group.status = next((work.status for work in preferred_works if work.status), None)
        group.year = next((work.year for work in preferred_works if work.year), None)
        group.language = next((work.language for work in preferred_works if work.language), None)
        group.tags = sorted({tag for work in works for tag in (work.tags or [])})
        dates = [
            as_utc(source.source_updated_at)
            for work in works
            for source in work.sources
            if source.source_updated_at
        ]
        group.first_source_at = min(dates, default=None)
        group.latest_source_at = max(dates, default=None)
        group.updated_at = datetime.now(UTC)

    def _upsert_fingerprint(self, work: Work, author_name: str) -> WorkFingerprint:
        normalized = normalize_title(work.title, author_name)
        aliases: set[str] = set()
        for source in work.sources:
            for value in self._strings(source.raw_metadata.get("altTitles", [])):
                alias = normalize_title(value, author_name)
                if alias and alias != normalized:
                    aliases.add(alias)
        labels = extract_variant_labels(work.title, work.language)
        page_counts = [
            source.raw_metadata.get("page_count")
            for source in work.sources
            if isinstance(source.raw_metadata.get("page_count"), int)
        ]
        page_count = max(page_counts, default=None)
        fingerprint = work.fingerprint
        if fingerprint is None:
            fingerprint = WorkFingerprint(
                work=work,
                normalized_title=normalized or work.title.casefold(),
                title_aliases=sorted(aliases),
                variant_labels=labels,
                page_count=page_count,
            )
            self.session.add(fingerprint)
        else:
            fingerprint.normalized_title = normalized or work.title.casefold()
            fingerprint.title_aliases = sorted(aliases)
            fingerprint.variant_labels = labels
            fingerprint.page_count = page_count
        self.session.flush()
        return fingerprint

    async def _ensure_cover_fingerprint(
        self, fingerprint: WorkFingerprint, cover_url: str | None
    ) -> None:
        if not self.cover_hasher or not cover_url:
            return
        current = fingerprint.cover_fingerprint
        if is_current_fingerprint(current) and current.get("source_url") == cover_url:
            return
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
            return
        computed["source_url"] = cover_url
        fingerprint.cover_fingerprint = computed
        variants = computed.get("variants", [])
        fingerprint.cover_hash = variants[0].get("dhash") if variants else None

    def _candidate_groups(self, author_id: int) -> list[WorkGroup]:
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
        self, fingerprint: WorkFingerprint, groups: list[WorkGroup]
    ) -> tuple[WorkGroup | None, float, bool]:
        best_group: WorkGroup | None = None
        best_score = 0.0
        exact = False
        names = self._fingerprint_names(fingerprint)
        for group in groups:
            if not self._group_numbering_matches(fingerprint, group):
                continue
            for member in group.members:
                other = member.work.fingerprint
                if not other:
                    continue
                other_names = self._fingerprint_names(other)
                overlap = names & other_names
                valid_exact = any(len(_compact(value)) >= 3 for value in overlap)
                score = best_identity_similarity(names, other_names)
                if valid_exact:
                    return group, 1.0, True
                if score > best_score:
                    best_group, best_score = group, score
        return best_group, best_score, exact

    def _best_fingerprint(
        self, fingerprint: WorkFingerprint, group: WorkGroup
    ) -> WorkFingerprint | None:
        names = self._fingerprint_names(fingerprint)
        candidates = [member.work.fingerprint for member in group.members]
        candidates = [candidate for candidate in candidates if candidate]
        return max(
            candidates,
            key=lambda candidate: best_identity_similarity(
                names, self._fingerprint_names(candidate)
            ),
            default=None,
        )

    def _best_cover_candidate(
        self, fingerprint: WorkFingerprint, groups: list[WorkGroup]
    ) -> tuple[WorkGroup | None, WorkFingerprint | None, int | None, str | None]:
        best_group: WorkGroup | None = None
        best_fingerprint: WorkFingerprint | None = None
        best_distance: int | None = None
        best_mode: str | None = None
        for group in groups:
            if not self._group_numbering_matches(fingerprint, group):
                continue
            for member in group.members:
                candidate = member.work.fingerprint
                if not candidate:
                    continue
                distance, mode, _ = cover_fingerprint_distance(fingerprint, candidate)
                if distance is not None and (best_distance is None or distance < best_distance):
                    best_group = group
                    best_fingerprint = candidate
                    best_distance = distance
                    best_mode = mode
        return best_group, best_fingerprint, best_distance, best_mode

    @staticmethod
    def _numbering_matches(
        left: WorkFingerprint, right: WorkFingerprint
    ) -> bool:
        left_numbers = identity_number_signature(left.normalized_title)
        right_numbers = identity_number_signature(right.normalized_title)
        return not left_numbers or not right_numbers or left_numbers == right_numbers

    @classmethod
    def _group_numbering_matches(
        cls,
        fingerprint: WorkFingerprint,
        group: WorkGroup,
        *,
        excluding_work_id: int | None = None,
    ) -> bool:
        return all(
            member.work.fingerprint is None
            or cls._numbering_matches(fingerprint, member.work.fingerprint)
            for member in group.members
            if member.work_id != excluding_work_id
        )

    def _detach_automatic_membership(self, membership: WorkGroupMember) -> None:
        group = membership.group
        group.members.remove(membership)
        self.session.delete(membership)
        self.session.flush()
        if group.members:
            self.recompute(group)
        else:
            self.session.delete(group)
            self.session.flush()

    def _reconcile_existing(
        self,
        work: Work,
        author: Author,
        fingerprint: WorkFingerprint,
        current_group: WorkGroup,
    ) -> WorkGroup:
        candidates = [
            group
            for group in self._candidate_groups(author.id)
            if group.id != current_group.id
        ]
        target, title_score, exact = self._best_title_candidate(
            fingerprint, candidates
        )
        if target and not any(member.is_manual for member in target.members):
            if exact:
                return self.merge_groups(
                    target,
                    current_group,
                    manual=False,
                    method="title_exact_reconcile",
                )
            if (
                title_score >= _TITLE_FUZZY_THRESHOLD
                and self._identity_length(fingerprint) >= 6
            ):
                return self.merge_groups(
                    target,
                    current_group,
                    manual=False,
                    method="title_fuzzy_reconcile",
                )
        return self._reconcile_by_cover(
            work, author, fingerprint, current_group
        )

    def _reconcile_by_cover(
        self,
        work: Work,
        author: Author,
        fingerprint: WorkFingerprint,
        current_group: WorkGroup,
    ) -> WorkGroup:
        candidates = [
            group for group in self._candidate_groups(author.id) if group.id != current_group.id
        ]
        target, candidate, distance, mode = self._best_cover_candidate(
            fingerprint, candidates
        )
        if not target or not candidate or distance is None:
            return current_group
        has_manual_members = any(
            member.is_manual for group in (target, current_group) for member in group.members
        )
        if (
            not has_manual_members
            and distance <= _STRONG_COVER_DISTANCE
            and self._page_counts_compatible(fingerprint, candidate)
            and (mode != "crop" or self._page_counts_match(fingerprint, candidate))
            and self._years_match(work, candidate.work)
        ):
            return self.merge_groups(
                target,
                current_group,
                manual=False,
                method="cover_number_author_reconcile",
            )
        if distance <= _SUGGEST_COVER_DISTANCE:
            self._suggest(
                current_group,
                target,
                0.76,
                [f"封面视觉距离 {distance}", "同一作者，标题语言或写法不同"],
            )
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
        return group

    def _attach(
        self, work: Work, group: WorkGroup, confidence: float, method: str
    ) -> WorkGroup:
        group.members.append(
            WorkGroupMember(
                work=work,
                confidence=confidence,
                match_method=method,
            )
        )
        self.session.flush()
        self.recompute(group)
        return group

    def _suggest(
        self, source: WorkGroup, target: WorkGroup, confidence: float, reasons: list[str]
    ) -> None:
        is_constrained = self.session.scalar(
            select(PairConstraint.id).where(
                PairConstraint.candidate_key == candidate_key(source, target),
                PairConstraint.decision == "different_work",
            )
        )
        if is_constrained is not None:
            return
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
            self.session.add(
                MergeSuggestion(
                    source_group_id=source.id,
                    target_group_id=target.id,
                    confidence=confidence,
                    reasons=reasons,
                )
            )
        else:
            existing.confidence = confidence
            existing.reasons = reasons

    def prune_pending_suggestions(self, author_id: int) -> int:
        """Drop stale fuzzy candidates that no longer pass the V2 retrieval policy."""
        from app.modules.agent_review.candidates import build_candidate_evidence

        group_ids = set(
            self.session.scalars(
                select(WorkGroupMember.group_id)
                .join(AuthorWork, AuthorWork.work_id == WorkGroupMember.work_id)
                .where(AuthorWork.author_id == author_id)
            )
        )
        suggestions = list(
            self.session.scalars(
                select(MergeSuggestion).where(MergeSuggestion.status == "pending")
            )
        )
        removed = 0
        for suggestion in suggestions:
            if (
                suggestion.source_group_id not in group_ids
                or suggestion.target_group_id not in group_ids
            ):
                continue
            source = self.session.get(WorkGroup, suggestion.source_group_id)
            target = self.session.get(WorkGroup, suggestion.target_group_id)
            if source is None or target is None:
                continue
            evidence = build_candidate_evidence(suggestion, source, target)
            title_support = bool(
                set(evidence.available_evidence)
                & {"core_title_match", "source_alias_match", "core_title_similarity"}
            )
            visual_support = bool(
                set(evidence.available_evidence)
                & {"cover_hash_strong", "cover_hash_weak", "cover_crop_match"}
            )
            if not title_support and not visual_support:
                self.session.delete(suggestion)
                removed += 1
        self.session.flush()
        return removed

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

    @classmethod
    def _identity_length(cls, fingerprint: WorkFingerprint) -> int:
        return len(_compact(fingerprint.normalized_title))

    @staticmethod
    def _page_counts_match(left: WorkFingerprint, right: WorkFingerprint) -> bool:
        if left.page_count is None or right.page_count is None:
            return False
        tolerance = max(2, round(max(left.page_count, right.page_count) * 0.03))
        return abs(left.page_count - right.page_count) <= tolerance

    @classmethod
    def _page_counts_compatible(
        cls, left: WorkFingerprint, right: WorkFingerprint
    ) -> bool:
        if left.page_count is None or right.page_count is None:
            return True
        return cls._page_counts_match(left, right)

    @staticmethod
    def _years_match(left: Work, right: Work) -> bool:
        return left.year is None or right.year is None or abs(left.year - right.year) <= 1

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
    for work, author in rows:
        if work.group_membership is None:
            service.assign_without_cover(work, author)
            created += 1
    session.commit()
    return created

import re
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db.models import Series, SeriesMember, WorkGroup, WorkRelation
from app.modules.catalog.group_repository import WorkGroupRepository
from app.modules.catalog.title_identity import normalize_identity_text

_NUMBERED_TITLE_PATTERNS = (
    re.compile(r"^(?P<base>.+?)\s*(?:vol(?:ume)?\.?|第)\s*(?P<number>\d{1,3})\s*巻?$", re.I),
    re.compile(r"^(?P<base>.+?)\s+(?P<number>\d{1,3})\s*巻$", re.I),
    re.compile(r"^(?P<base>.+?)\s*#\s*(?P<number>\d{1,3})$", re.I),
)
RELATION_TYPES = {"prequel", "sequel", "side_story", "compilation"}


@dataclass(frozen=True, slots=True)
class NumberedTitle:
    base: str
    normalized_base: str
    number: int


@dataclass(frozen=True, slots=True)
class SeriesCandidate:
    title: str
    normalized_title: str
    author_ids: tuple[int, ...]
    groups: tuple[tuple[int, str, int], ...]
    missing_volumes: tuple[int, ...]


def parse_numbered_title(title: str) -> NumberedTitle | None:
    for pattern in _NUMBERED_TITLE_PATTERNS:
        match = pattern.match(title.strip())
        if not match:
            continue
        base = match.group("base").strip(" -_·:：")
        number = int(match.group("number"))
        normalized = normalize_identity_text(base)
        if normalized and 0 < number <= 999:
            return NumberedTitle(base=base, normalized_base=normalized, number=number)
    return None


class SeriesService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def candidates(self, author_id: int | None = None) -> list[SeriesCandidate]:
        buckets: dict[
            tuple[str, tuple[int, ...]], list[tuple[WorkGroup, NumberedTitle]]
        ] = defaultdict(list)
        for group in WorkGroupRepository(self.session).list_groups(author_id):
            parsed = parse_numbered_title(group.title)
            if not parsed:
                continue
            author_ids = tuple(
                sorted(
                    {
                        author.id
                        for member in group.members
                        for author in member.work.authors
                    }
                )
            )
            if not author_ids:
                continue
            buckets[(parsed.normalized_base, author_ids)].append((group, parsed))

        result: list[SeriesCandidate] = []
        for (normalized, author_ids), items in buckets.items():
            distinct_numbers = {parsed.number for _, parsed in items}
            if len(items) < 2 or len(distinct_numbers) != len(items):
                continue
            ordered = sorted(items, key=lambda item: item[1].number)
            low, high = min(distinct_numbers), max(distinct_numbers)
            missing = tuple(
                number
                for number in range(low, high + 1)
                if number not in distinct_numbers
            )
            result.append(
                SeriesCandidate(
                    title=ordered[0][1].base,
                    normalized_title=normalized,
                    author_ids=author_ids,
                    groups=tuple(
                        (group.id, group.title, parsed.number)
                        for group, parsed in ordered
                    ),
                    missing_volumes=missing,
                )
            )
        return sorted(result, key=lambda item: item.normalized_title)

    def create(
        self, title: str, members: list[tuple[int, int | None]], *, manual: bool
    ) -> Series:
        normalized = normalize_identity_text(title)
        if not normalized:
            raise ValueError("系列标题不能为空")
        series = Series(title=title.strip(), normalized_title=normalized)
        self.session.add(series)
        self.session.flush()
        for group_id, volume_number in members:
            self.set_member(series.id, group_id, volume_number, manual=manual)
        self.session.flush()
        return series

    def set_member(
        self,
        series_id: int,
        group_id: int,
        volume_number: int | None,
        *,
        label: str | None = None,
        manual: bool = True,
    ) -> SeriesMember:
        if not self.session.get(Series, series_id):
            raise LookupError("系列不存在")
        if not self.session.get(WorkGroup, group_id):
            raise LookupError("作品不存在")
        existing = self.session.scalar(
            select(SeriesMember).where(SeriesMember.group_id == group_id)
        )
        if existing and existing.series_id != series_id:
            self.session.delete(existing)
            self.session.flush()
            existing = None
        member = existing or SeriesMember(series_id=series_id, group_id=group_id)
        if existing is None:
            self.session.add(member)
        member.volume_number = volume_number
        member.label = label
        member.is_manual = manual
        member.confidence = 1.0 if manual else 0.9
        self.session.flush()
        return member

    def remove_member(self, series_id: int, group_id: int) -> bool:
        member = self.session.get(SeriesMember, (series_id, group_id))
        if not member:
            return False
        self.session.delete(member)
        self.session.flush()
        return True

    def add_relation(
        self, source_group_id: int, target_group_id: int, relation_type: str
    ) -> WorkRelation:
        if relation_type not in RELATION_TYPES:
            raise ValueError("不支持的作品关系")
        if source_group_id == target_group_id:
            raise ValueError("作品不能关联自身")
        if not self.session.get(WorkGroup, source_group_id) or not self.session.get(
            WorkGroup, target_group_id
        ):
            raise LookupError("作品不存在")
        relation = self.session.scalar(
            select(WorkRelation).where(
                WorkRelation.source_group_id == source_group_id,
                WorkRelation.target_group_id == target_group_id,
                WorkRelation.relation_type == relation_type,
            )
        )
        if relation:
            return relation
        relation = WorkRelation(
            source_group_id=source_group_id,
            target_group_id=target_group_id,
            relation_type=relation_type,
        )
        self.session.add(relation)
        self.session.flush()
        return relation

    def relations_for_group(self, group_id: int) -> list[WorkRelation]:
        return list(
            self.session.scalars(
                select(WorkRelation)
                .where(
                    or_(
                        WorkRelation.source_group_id == group_id,
                        WorkRelation.target_group_id == group_id,
                    )
                )
                .order_by(WorkRelation.created_at, WorkRelation.id)
            )
        )

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.time import utc_timestamp
from app.db.models import (
    AuthorWork,
    MergeSuggestion,
    Work,
    WorkGroup,
    WorkGroupMember,
)


class WorkGroupRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list_groups(self, author_id: int | None = None) -> list[WorkGroup]:
        statement = select(WorkGroup).options(
            selectinload(WorkGroup.members)
            .selectinload(WorkGroupMember.work)
            .selectinload(Work.sources),
            selectinload(WorkGroup.members)
            .selectinload(WorkGroupMember.work)
            .selectinload(Work.fingerprint),
            selectinload(WorkGroup.members)
            .selectinload(WorkGroupMember.work)
            .selectinload(Work.authors),
        )
        if author_id is not None:
            statement = (
                statement.join(WorkGroupMember)
                .join(AuthorWork, AuthorWork.work_id == WorkGroupMember.work_id)
                .where(AuthorWork.author_id == author_id)
                .distinct()
            )
        groups = list(self.session.scalars(statement))
        groups.sort(key=lambda group: group.title.casefold())
        groups.sort(key=lambda group: self._timestamp(group.latest_source_at), reverse=True)
        groups.sort(key=lambda group: group.latest_source_at is None)
        return groups

    def get(self, group_id: int) -> WorkGroup | None:
        return self.session.scalar(
            select(WorkGroup)
            .options(
                selectinload(WorkGroup.members)
                .selectinload(WorkGroupMember.work)
                .selectinload(Work.sources),
                selectinload(WorkGroup.members)
                .selectinload(WorkGroupMember.work)
                .selectinload(Work.fingerprint),
                selectinload(WorkGroup.members)
                .selectinload(WorkGroupMember.work)
                .selectinload(Work.authors),
            )
            .where(WorkGroup.id == group_id)
        )

    def suggestions(self, status: str = "pending") -> list[MergeSuggestion]:
        return list(
            self.session.scalars(
                select(MergeSuggestion)
                .where(MergeSuggestion.status == status)
                .order_by(MergeSuggestion.confidence.desc())
            )
        )

    def suggestion(self, suggestion_id: int) -> MergeSuggestion | None:
        return self.session.get(MergeSuggestion, suggestion_id)

    @staticmethod
    def _timestamp(value: datetime | None) -> float:
        if value is None:
            return 0.0
        return utc_timestamp(value)

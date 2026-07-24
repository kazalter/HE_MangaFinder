from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    Author,
    AuthorWork,
    AuthorWorkExclusion,
    Work,
    WorkGroup,
)
from app.modules.catalog.aggregation import AggregationService


class WorkExclusionService:
    """Remove an incorrect author/work match and keep it removed on later scans."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def exclude_work(self, author: Author, work: Work) -> WorkGroup | None:
        link = self.session.get(AuthorWork, (author.id, work.id))
        if link is None:
            raise LookupError("该版本不属于所选作者")

        for source in work.sources:
            existing = self.session.scalar(
                select(AuthorWorkExclusion).where(
                    AuthorWorkExclusion.author_id == author.id,
                    AuthorWorkExclusion.provider == source.provider,
                    AuthorWorkExclusion.external_id == source.external_id,
                )
            )
            if existing is None:
                self.session.add(
                    AuthorWorkExclusion(
                        author_id=author.id,
                        provider=source.provider,
                        external_id=source.external_id,
                        title=work.title,
                        source_url=source.source_url,
                    )
                )

        self.session.delete(link)
        self.session.flush()
        if self.session.scalar(
            select(AuthorWork.work_id).where(AuthorWork.work_id == work.id).limit(1)
        ) is not None:
            return work.group_membership.group if work.group_membership else None

        membership = work.group_membership
        group = membership.group if membership else None
        self.session.delete(work)
        self.session.flush()
        if group is None:
            return None
        self.session.expire(group, ["members"])
        if group.members:
            AggregationService(self.session).recompute(group)
            return group
        self.session.delete(group)
        self.session.flush()
        return None

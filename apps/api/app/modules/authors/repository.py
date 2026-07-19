from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.db.models import Author, AuthorWork, SocialAccount, WorkGroupMember


class AuthorRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    @staticmethod
    def _confirmed_x_value(column: ColumnElement[Any]) -> Any:
        return (
            select(column)
            .where(
                SocialAccount.author_id == Author.id,
                SocialAccount.platform == "x",
                SocialAccount.status == "confirmed",
            )
            .order_by(SocialAccount.created_at, SocialAccount.id)
            .limit(1)
            .correlate(Author)
            .scalar_subquery()
        )

    def list_with_counts(
        self,
    ) -> list[
        tuple[
            Author,
            int,
            str | None,
            str | None,
            str | None,
            datetime | None,
            str | None,
        ]
    ]:
        avatar_url = self._confirmed_x_value(SocialAccount.avatar_url)
        x_handle = self._confirmed_x_value(SocialAccount.handle)
        x_display_name = self._confirmed_x_value(SocialAccount.display_name)
        x_last_synced_at = self._confirmed_x_value(SocialAccount.last_synced_at)
        x_sync_error = self._confirmed_x_value(SocialAccount.sync_error)
        statement = (
            select(
                Author,
                func.count(func.distinct(WorkGroupMember.group_id)),
                avatar_url,
                x_handle,
                x_display_name,
                x_last_synced_at,
                x_sync_error,
            )
            .outerjoin(AuthorWork, AuthorWork.author_id == Author.id)
            .outerjoin(WorkGroupMember, WorkGroupMember.work_id == AuthorWork.work_id)
            .group_by(Author.id)
            .order_by(Author.created_at.desc())
        )
        return [
            row
            for row in self.session.execute(statement).all()
        ]

    def get(self, author_id: int) -> Author | None:
        return self.session.get(Author, author_id)

    def find_by_name(self, name: str) -> Author | None:
        return self.session.scalar(select(Author).where(func.lower(Author.name) == name.lower()))

    def add(self, name: str) -> Author:
        author = Author(name=name)
        self.session.add(author)
        self.session.flush()
        return author

    def delete(self, author: Author) -> None:
        self.session.delete(author)

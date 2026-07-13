from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Author, AuthorWork, WorkGroupMember


class AuthorRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list_with_counts(self) -> list[tuple[Author, int]]:
        statement = (
            select(Author, func.count(func.distinct(WorkGroupMember.group_id)))
            .outerjoin(AuthorWork, AuthorWork.author_id == Author.id)
            .outerjoin(WorkGroupMember, WorkGroupMember.work_id == AuthorWork.work_id)
            .group_by(Author.id)
            .order_by(Author.created_at.desc())
        )
        return [(author, count) for author, count in self.session.execute(statement).all()]

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

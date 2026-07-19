from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models import Author, SocialAccount
from app.modules.authors.repository import AuthorRepository


def test_author_list_uses_only_confirmed_x_account_avatar() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        confirmed = Author(name="Confirmed")
        suggested = Author(name="Suggested")
        session.add_all([confirmed, suggested])
        session.flush()
        session.add_all(
            [
                SocialAccount(
                    author_id=confirmed.id,
                    platform="x",
                    handle="confirmed",
                    status="confirmed",
                    avatar_url="https://pbs.twimg.com/confirmed.jpg",
                ),
                SocialAccount(
                    author_id=suggested.id,
                    platform="x",
                    handle="suggested",
                    status="suggested",
                    avatar_url="https://pbs.twimg.com/suggested.jpg",
                ),
            ]
        )
        session.commit()

        rows = AuthorRepository(session).list_with_counts()

    avatars = {author.name: avatar for author, _, avatar, *_ in rows}
    assert avatars == {
        "Confirmed": "https://pbs.twimg.com/confirmed.jpg",
        "Suggested": None,
    }

    metadata = {
        author.name: (handle, display_name, last_synced_at, sync_error)
        for author, _, _, handle, display_name, last_synced_at, sync_error in rows
    }
    assert metadata["Confirmed"][0] == "confirmed"
    assert metadata["Suggested"] == (None, None, None, None)

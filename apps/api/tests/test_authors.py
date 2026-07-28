import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models import Author, SocialAccount
from app.modules.authors.repository import AuthorRepository
from app.modules.authors.router import list_authors, update_author_identity
from app.modules.authors.schemas import AuthorIdentityUpdate


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


def test_author_api_exposes_only_same_origin_avatar_url() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        author = Author(name="Local avatar")
        session.add(author)
        session.flush()
        session.add(
            SocialAccount(
                author_id=author.id,
                platform="x",
                handle="local_avatar",
                status="confirmed",
                avatar_url="https://pbs.twimg.com/profile_images/1/avatar.jpg",
            )
        )
        session.commit()
        author_id = author.id

        result = list_authors(session)

    assert result[0].avatar_url == f"/api/authors/{author_id}/avatar"


def test_identity_conflicts_require_explicit_human_confirmation() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        first = Author(name="Canonical", aliases=["Shared Pen Name"])
        second = Author(name="Second")
        session.add_all([first, second])
        session.commit()

        payload = AuthorIdentityUpdate(
            aliases=["Shared Pen Name", "Second Alias"],
            circle_names=["Circle"],
        )
        with pytest.raises(HTTPException) as error:
            update_author_identity(second.id, payload, session)
        assert error.value.status_code == 409

        updated = update_author_identity(
            second.id,
            payload.model_copy(update={"confirm_conflicts": True}),
            session,
        )
        assert updated.aliases == ["Shared Pen Name", "Second Alias"]
        assert updated.circle_names == ["Circle"]

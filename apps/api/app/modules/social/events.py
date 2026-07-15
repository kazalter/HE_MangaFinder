from sqlalchemy.orm import Session

from app.db.models import EventRegistry


def seed_event_registry(session: Session) -> None:
    """Seed identifiers only; dates must come from official event documents."""
    for number in range(100, 111):
        code = f"C{number}"
        if session.get(EventRegistry, code):
            continue
        session.add(
            EventRegistry(
                code=code,
                name=f"Comic Market {number}",
                aliases=[code, f"コミケ{number}", f"コミックマーケット{number}"],
                source_url="https://www.comiket.co.jp/",
            )
        )
    session.commit()

from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, event, inspect, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.db.base import Base

settings = get_settings()

if settings.database_url.startswith("sqlite:///"):
    database_path = Path(settings.database_url.removeprefix("sqlite:///"))
    database_path.parent.mkdir(parents=True, exist_ok=True)

connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args)

if settings.database_url.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def enable_sqlite_foreign_keys(dbapi_connection: object, _: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    _apply_additive_migrations()
    # Additive, idempotent migration: legacy source records remain untouched.
    from app.db.models import WorkFingerprint
    from app.modules.catalog.aggregation import backfill_work_groups
    from app.modules.catalog.repairs import repair_wnacg_upload_years
    from app.modules.jobs.repository import JobRepository
    from app.modules.social.events import seed_event_registry

    with Session(engine) as session:
        repair_wnacg_upload_years(session)
        backfill_work_groups(session)
        seed_event_registry(session)
        if session.scalar(
            select(WorkFingerprint.work_id).where(
                WorkFingerprint.cover_fingerprint.is_(None)
            ).limit(1)
        ) is not None:
            JobRepository(session).enqueue_cover_fingerprint_refresh()
            session.commit()


def _apply_additive_migrations() -> None:
    """Keep existing SQLite installations compatible without destructive migrations."""
    columns = {
        item["name"] for item in inspect(engine).get_columns("work_fingerprints")
    }
    if "cover_fingerprint" not in columns:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "ALTER TABLE work_fingerprints ADD COLUMN cover_fingerprint JSON"
            )


def get_session() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session

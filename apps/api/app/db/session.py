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
    from app.modules.agent_review.repairs import repair_historical_rationales
    from app.modules.catalog.aggregation import backfill_work_groups, prune_invalid_suggestions
    from app.modules.catalog.repairs import repair_wnacg_upload_years
    from app.modules.jobs.repository import JobRepository
    from app.modules.social.events import seed_event_registry

    with Session(engine) as session:
        repair_wnacg_upload_years(session)
        backfill_work_groups(session)
        prune_invalid_suggestions(session)
        if repair_historical_rationales(session):
            session.commit()
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
    fingerprint_columns = {
        item["name"] for item in inspect(engine).get_columns("work_fingerprints")
    }
    if "cover_fingerprint" not in fingerprint_columns:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "ALTER TABLE work_fingerprints ADD COLUMN cover_fingerprint JSON"
            )

    group_columns = {item["name"] for item in inspect(engine).get_columns("work_groups")}
    if "first_source_at" not in group_columns:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "ALTER TABLE work_groups ADD COLUMN first_source_at DATETIME"
            )
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE work_groups SET first_source_at = ("
            "SELECT MIN(work_sources.source_updated_at) "
            "FROM work_group_members "
            "JOIN work_sources ON work_sources.work_id = work_group_members.work_id "
            "WHERE work_group_members.group_id = work_groups.id"
            ") WHERE first_source_at IS NULL"
        )

    job_columns = {item["name"] for item in inspect(engine).get_columns("jobs")}
    if "next_attempt_at" not in job_columns:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "ALTER TABLE jobs ADD COLUMN next_attempt_at DATETIME"
            )
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_jobs_next_attempt_at "
                "ON jobs (next_attempt_at)"
            )


def get_session() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session

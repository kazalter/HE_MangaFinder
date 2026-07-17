from datetime import datetime

from sqlalchemy import DateTime
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.types import TypeDecorator

from app.core.time import as_utc


class UTCDateTime(TypeDecorator[datetime]):
    """Store UTC and restore timezone information that SQLite discards."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(
        self, value: datetime | None, dialect: Dialect
    ) -> datetime | None:
        return as_utc(value) if value is not None else None

    def process_result_value(
        self, value: datetime | None, dialect: Dialect
    ) -> datetime | None:
        return as_utc(value) if value is not None else None

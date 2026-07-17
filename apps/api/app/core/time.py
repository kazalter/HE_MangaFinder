from datetime import UTC, datetime


def as_utc(value: datetime) -> datetime:
    """Return an aware UTC datetime, treating SQLite's naive values as UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def utc_timestamp(value: datetime) -> float:
    """Return a stable timestamp for either SQLite-naive or timezone-aware values."""
    return as_utc(value).timestamp()

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.db.models import JobStatus


class JobRead(BaseModel):
    id: int
    kind: str
    payload: dict[str, Any]
    status: JobStatus
    attempts: int
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    next_attempt_at: datetime | None

    model_config = ConfigDict(from_attributes=True)

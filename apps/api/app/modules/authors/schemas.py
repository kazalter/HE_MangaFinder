from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AuthorCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not cleaned:
            raise ValueError("作者名不能为空")
        return cleaned


class AuthorRead(BaseModel):
    id: int
    name: str
    created_at: datetime
    last_checked_at: datetime | None
    work_count: int = 0

    model_config = ConfigDict(from_attributes=True)

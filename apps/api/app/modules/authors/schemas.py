from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AuthorCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    aliases: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not cleaned:
            raise ValueError("作者名不能为空")
        return cleaned

    @field_validator("aliases")
    @classmethod
    def clean_aliases(cls, values: list[str]) -> list[str]:
        cleaned = [" ".join(v.split()) for v in values]
        return list(dict.fromkeys(v for v in cleaned if v))


class AuthorIdentityUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    aliases: list[str] | None = Field(default=None, max_length=100)
    circle_names: list[str] | None = Field(default=None, max_length=100)
    provider_query_names: dict[str, str] | None = None
    identity_note: str | None = Field(default=None, max_length=2000)
    confirm_conflicts: bool = False

    @field_validator("name")
    @classmethod
    def clean_optional_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = " ".join(value.split())
        if not cleaned:
            raise ValueError("作者名不能为空")
        return cleaned

    @field_validator("aliases", "circle_names")
    @classmethod
    def clean_names(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        cleaned = [" ".join(value.split()) for value in values]
        return list(dict.fromkeys(value for value in cleaned if value))

    @field_validator("provider_query_names")
    @classmethod
    def clean_query_names(
        cls, values: dict[str, str] | None
    ) -> dict[str, str] | None:
        if values is None:
            return None
        result: dict[str, str] = {}
        for provider, value in values.items():
            key = provider.strip().casefold()
            name = " ".join(value.split())
            if key and name:
                result[key] = name
        return result


class AuthorQueryNameRead(BaseModel):
    provider: str
    query_name: str
    origin: str
    explanation: str
    support: int = 0


class AuthorRead(BaseModel):
    id: int
    name: str
    aliases: list[str] = Field(default_factory=list)
    circle_names: list[str] = Field(default_factory=list)
    provider_query_names: dict[str, str] = Field(default_factory=dict)
    identity_note: str | None = None
    avatar_url: str | None = None
    x_handle: str | None = None
    x_display_name: str | None = None
    x_last_synced_at: datetime | None = None
    x_sync_error: str | None = None
    created_at: datetime
    last_checked_at: datetime | None
    work_count: int = 0

    model_config = ConfigDict(from_attributes=True)

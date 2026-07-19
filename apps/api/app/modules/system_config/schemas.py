from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class AuthStatus(BaseModel):
    initialized: bool
    authenticated: bool
    username: str | None = None


class PasswordInput(BaseModel):
    password: str = Field(min_length=12, max_length=256)


class AiConfig(BaseModel):
    enabled: bool
    provider: Literal["openai_compatible", "deepseek"]
    base_url: str = Field(min_length=1, max_length=500)
    model: str = Field(max_length=200)
    api_key: str | None = Field(default=None, max_length=1000)
    api_key_configured: bool = False
    temperature: float = Field(ge=0, le=2)
    timeout_seconds: float = Field(ge=5, le=300)
    review_after_discovery: bool


class RadarConfig(BaseModel):
    enabled: bool
    sync_interval_minutes: int = Field(ge=5, le=10080)
    event_sync_interval_minutes: int = Field(ge=5, le=1440)
    initial_backfill_days: int = Field(ge=1, le=365)
    max_posts_per_sync: int = Field(ge=1, le=500)
    agent_enabled: bool
    candidate_threshold: float = Field(ge=0, le=1)
    auto_confirm_threshold: float = Field(ge=0, le=1)
    ocr_enabled: bool
    ocr_max_posts_per_sync: int = Field(ge=0, le=100)
    ocr_timeout_seconds: float = Field(ge=5, le=180)
    media_cache_max_gb: int = Field(ge=1, le=1000)
    media_max_dimension: int = Field(ge=640, le=4096)
    media_webp_quality: int = Field(ge=40, le=95)
    delete_confirm_hours: int = Field(ge=1, le=168)

    @model_validator(mode="after")
    def thresholds_are_ordered(self) -> "RadarConfig":
        if self.auto_confirm_threshold < self.candidate_threshold:
            raise ValueError("自动确认阈值不能低于候选阈值")
        return self


class NotificationConfig(BaseModel):
    daily_digest_enabled: bool
    daily_digest_hour: int = Field(ge=0, le=23)
    daily_digest_timezone: str = Field(min_length=1, max_length=100)
    daily_digest_initial_lookback_days: int = Field(ge=1, le=90)
    daily_digest_min_importance: Literal["low", "normal", "high"]
    daily_digest_max_authors: int = Field(ge=1, le=100)
    daily_digest_max_items_per_author: int = Field(ge=1, le=20)
    qq_enabled: bool
    qq_app_id: str = Field(max_length=200)
    qq_client_secret: str | None = Field(default=None, max_length=1000)
    qq_client_secret_configured: bool = False
    qq_user_openid: str = Field(max_length=300)


class XSessionStatus(BaseModel):
    configured: bool
    collector_reachable: bool
    valid: bool | None = None
    provider: str | None = None
    last_error: str | None = None
    proxy_configured: bool
    user_agent_configured: bool


class DeploymentStatus(BaseModel):
    api_running: bool = True
    collector_running: bool
    social_profile_required: bool
    x_session_dir: str
    social_media_dir: str
    database_url: str
    restart_required_fields: list[str] = Field(default_factory=list)


class SystemConfigRead(BaseModel):
    ai: AiConfig
    radar: RadarConfig
    notifications: NotificationConfig
    x_session: XSessionStatus
    deployment: DeploymentStatus


class SystemConfigUpdate(BaseModel):
    ai: AiConfig
    radar: RadarConfig
    notifications: NotificationConfig


class SaveResult(BaseModel):
    config: SystemConfigRead
    changed_keys: list[str]
    restart_required_fields: list[str]


class ConnectionTestResult(BaseModel):
    ok: bool
    message: str


class AiConnectionInput(BaseModel):
    provider: Literal["openai_compatible", "deepseek"]
    base_url: str = Field(min_length=1, max_length=500)
    model: str = Field(max_length=200)
    api_key: str | None = Field(default=None, max_length=1000)


class QqConnectionInput(BaseModel):
    app_id: str = Field(max_length=200)
    client_secret: str | None = Field(default=None, max_length=1000)
    user_openid: str = Field(max_length=300)


class XSessionImport(BaseModel):
    cookie_header: str | None = Field(default=None, max_length=20000)
    storage_state: dict[str, Any] | None = None

    @model_validator(mode="after")
    def exactly_one_source(self) -> "XSessionImport":
        if bool(self.cookie_header) == bool(self.storage_state):
            raise ValueError("请选择粘贴 Cookie 或上传 storage-state.json 其中一种方式")
        return self

import json
import os
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import ConfigAuditEvent, SystemSetting

RUNTIME_FIELDS = {
    "agent_enabled",
    "agent_provider",
    "agent_base_url",
    "agent_model",
    "agent_temperature",
    "agent_timeout_seconds",
    "agent_review_after_discovery",
    "social_enabled",
    "social_sync_interval_minutes",
    "social_event_sync_interval_minutes",
    "social_initial_backfill_days",
    "social_max_posts_per_sync",
    "social_agent_enabled",
    "social_auto_confirm_threshold",
    "social_candidate_threshold",
    "social_ocr_enabled",
    "social_ocr_max_posts_per_sync",
    "social_ocr_timeout_seconds",
    "social_daily_digest_enabled",
    "social_daily_digest_hour",
    "social_daily_digest_timezone",
    "social_daily_digest_initial_lookback_days",
    "social_daily_digest_min_importance",
    "social_daily_digest_max_authors",
    "social_daily_digest_max_items_per_author",
    "qq_bot_enabled",
    "qq_bot_app_id",
    "qq_bot_user_openid",
}
SECRET_FIELDS = {"agent_api_key", "qq_bot_client_secret"}


class SecretBox:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _fernet(self) -> Fernet:
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
            temporary.write_bytes(Fernet.generate_key())
            os.chmod(temporary, 0o600)
            temporary.replace(self.path)
        os.chmod(self.path, 0o600)
        return Fernet(self.path.read_bytes().strip())

    def encrypt(self, value: str) -> str:
        return self._fernet().encrypt(value.encode()).decode()

    def decrypt(self, value: str) -> str:
        try:
            return self._fernet().decrypt(value.encode()).decode()
        except InvalidToken as exc:
            raise RuntimeError("系统设置加密密钥与数据库中的凭证不匹配") from exc


class SystemSettingRepository:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.box = SecretBox(settings.system_secret_key_file)

    def overrides(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for item in self.session.scalars(select(SystemSetting)):
            raw = self.box.decrypt(item.value) if item.encrypted else item.value
            try:
                result[item.key] = json.loads(raw)
            except json.JSONDecodeError:
                result[item.key] = raw
        return result

    def set_value(self, key: str, value: Any, *, encrypted: bool = False) -> None:
        raw = json.dumps(value, ensure_ascii=False)
        stored = self.box.encrypt(raw) if encrypted else raw
        item = self.session.get(SystemSetting, key)
        if item:
            item.value = stored
            item.encrypted = encrypted
        else:
            self.session.add(SystemSetting(key=key, value=stored, encrypted=encrypted))

    def audit(self, account_id: int | None, action: str, changed_keys: list[str]) -> None:
        self.session.add(
            ConfigAuditEvent(
                account_id=account_id,
                action=action,
                changed_keys=sorted(changed_keys),
            )
        )


def apply_runtime_overrides(session: Session, settings: Settings) -> None:
    repository = SystemSettingRepository(session, settings)
    for key, value in repository.overrides().items():
        if key in RUNTIME_FIELDS or key in SECRET_FIELDS:
            setattr(settings, key, value)

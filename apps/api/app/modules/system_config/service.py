import json
import os
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import AdminAccount
from app.modules.social.collector import XBrowserCollector
from app.modules.social.notifications import QqBotClient
from app.modules.system_config.repository import (
    RUNTIME_FIELDS,
    SECRET_FIELDS,
    SystemSettingRepository,
)
from app.modules.system_config.schemas import (
    AiConfig,
    AiConnectionInput,
    ConnectionTestResult,
    DeploymentStatus,
    NotificationConfig,
    QqConnectionInput,
    RadarConfig,
    SaveResult,
    SystemConfigRead,
    SystemConfigUpdate,
    XSessionImport,
    XSessionStatus,
)


def _ai(settings: Settings) -> AiConfig:
    return AiConfig(
        enabled=settings.agent_enabled,
        provider=settings.agent_provider,
        base_url=settings.agent_base_url,
        model=settings.agent_model,
        api_key=None,
        api_key_configured=bool(settings.agent_api_key),
        temperature=settings.agent_temperature,
        timeout_seconds=settings.agent_timeout_seconds,
        review_after_discovery=settings.agent_review_after_discovery,
    )


def _radar(settings: Settings) -> RadarConfig:
    return RadarConfig(
        enabled=settings.social_enabled,
        sync_interval_minutes=settings.social_sync_interval_minutes,
        event_sync_interval_minutes=settings.social_event_sync_interval_minutes,
        initial_backfill_days=settings.social_initial_backfill_days,
        max_posts_per_sync=settings.social_max_posts_per_sync,
        agent_enabled=settings.social_agent_enabled,
        candidate_threshold=settings.social_candidate_threshold,
        auto_confirm_threshold=settings.social_auto_confirm_threshold,
        ocr_enabled=settings.social_ocr_enabled,
        ocr_max_posts_per_sync=settings.social_ocr_max_posts_per_sync,
        ocr_timeout_seconds=settings.social_ocr_timeout_seconds,
        media_cache_max_gb=max(
            1, round(settings.social_media_cache_max_bytes / (1024**3))
        ),
        media_max_dimension=settings.social_media_max_dimension,
        media_webp_quality=settings.social_media_webp_quality,
        delete_confirm_hours=settings.social_post_delete_confirm_hours,
    )


def _notifications(settings: Settings) -> NotificationConfig:
    return NotificationConfig(
        daily_digest_enabled=settings.social_daily_digest_enabled,
        daily_digest_hour=settings.social_daily_digest_hour,
        daily_digest_timezone=settings.social_daily_digest_timezone,
        daily_digest_initial_lookback_days=settings.social_daily_digest_initial_lookback_days,
        daily_digest_min_importance=settings.social_daily_digest_min_importance,
        daily_digest_max_authors=settings.social_daily_digest_max_authors,
        daily_digest_max_items_per_author=settings.social_daily_digest_max_items_per_author,
        qq_enabled=settings.qq_bot_enabled,
        qq_app_id=settings.qq_bot_app_id,
        qq_client_secret=None,
        qq_client_secret_configured=bool(settings.qq_bot_client_secret),
        qq_user_openid=settings.qq_bot_user_openid,
    )


async def _x_status(settings: Settings) -> XSessionStatus:
    source = settings.x_session_dir / "storage-state.json"
    runtime = settings.x_session_dir / "runtime-state.json"
    try:
        health = await XBrowserCollector(settings).health()
        return XSessionStatus(
            configured=bool(health.get("session_present")) or source.exists() or runtime.exists(),
            collector_reachable=True,
            valid=health.get("session_valid"),
            provider=str(health.get("primary_provider") or "browser"),
            proxy_configured=bool(settings.x_proxy_url),
            user_agent_configured=bool(settings.x_user_agent),
        )
    except RuntimeError as exc:
        return XSessionStatus(
            configured=source.exists() or runtime.exists(),
            collector_reachable=False,
            last_error=str(exc),
            proxy_configured=bool(settings.x_proxy_url),
            user_agent_configured=bool(settings.x_user_agent),
        )


async def read_config(
    settings: Settings, *, restart_required_fields: list[str] | None = None
) -> SystemConfigRead:
    x_status = await _x_status(settings)
    return SystemConfigRead(
        ai=_ai(settings),
        radar=_radar(settings),
        notifications=_notifications(settings),
        x_session=x_status,
        deployment=DeploymentStatus(
            collector_running=x_status.collector_reachable,
            social_profile_required=not x_status.collector_reachable,
            x_session_dir=str(settings.x_session_dir),
            social_media_dir=str(settings.social_media_dir),
            database_url=(
                settings.database_url
                if settings.database_url.startswith("sqlite")
                else settings.database_url.split("://", 1)[0] + "://…"
            ),
            restart_required_fields=restart_required_fields or [],
        ),
    )


def _values(payload: SystemConfigUpdate) -> dict[str, Any]:
    return {
        "agent_enabled": payload.ai.enabled,
        "agent_provider": payload.ai.provider,
        "agent_base_url": payload.ai.base_url.strip().rstrip("/"),
        "agent_model": payload.ai.model.strip(),
        "agent_temperature": payload.ai.temperature,
        "agent_timeout_seconds": payload.ai.timeout_seconds,
        "agent_review_after_discovery": payload.ai.review_after_discovery,
        "social_enabled": payload.radar.enabled,
        "social_sync_interval_minutes": payload.radar.sync_interval_minutes,
        "social_event_sync_interval_minutes": payload.radar.event_sync_interval_minutes,
        "social_initial_backfill_days": payload.radar.initial_backfill_days,
        "social_max_posts_per_sync": payload.radar.max_posts_per_sync,
        "social_agent_enabled": payload.radar.agent_enabled,
        "social_candidate_threshold": payload.radar.candidate_threshold,
        "social_auto_confirm_threshold": payload.radar.auto_confirm_threshold,
        "social_ocr_enabled": payload.radar.ocr_enabled,
        "social_ocr_max_posts_per_sync": payload.radar.ocr_max_posts_per_sync,
        "social_ocr_timeout_seconds": payload.radar.ocr_timeout_seconds,
        "social_media_cache_max_bytes": payload.radar.media_cache_max_gb * 1024**3,
        "social_media_max_dimension": payload.radar.media_max_dimension,
        "social_media_webp_quality": payload.radar.media_webp_quality,
        "social_post_delete_confirm_hours": payload.radar.delete_confirm_hours,
        "social_daily_digest_enabled": payload.notifications.daily_digest_enabled,
        "social_daily_digest_hour": payload.notifications.daily_digest_hour,
        "social_daily_digest_timezone": payload.notifications.daily_digest_timezone.strip(),
        "social_daily_digest_initial_lookback_days": (
            payload.notifications.daily_digest_initial_lookback_days
        ),
        "social_daily_digest_min_importance": (
            payload.notifications.daily_digest_min_importance
        ),
        "social_daily_digest_max_authors": payload.notifications.daily_digest_max_authors,
        "social_daily_digest_max_items_per_author": (
            payload.notifications.daily_digest_max_items_per_author
        ),
        "qq_bot_enabled": payload.notifications.qq_enabled,
        "qq_bot_app_id": payload.notifications.qq_app_id.strip(),
        "qq_bot_user_openid": payload.notifications.qq_user_openid.strip(),
    }


async def save_config(
    session: Session,
    settings: Settings,
    account: AdminAccount,
    payload: SystemConfigUpdate,
) -> SaveResult:
    repository = SystemSettingRepository(session, settings)
    changed: list[str] = []
    restart: list[str] = []
    for key, value in _values(payload).items():
        if key not in RUNTIME_FIELDS or getattr(settings, key) == value:
            continue
        repository.set_value(key, value)
        setattr(settings, key, value)
        changed.append(key)
        if key == "social_enabled":
            restart.append("作者动态调度器")
    secret_inputs = {
        "agent_api_key": payload.ai.api_key,
        "qq_bot_client_secret": payload.notifications.qq_client_secret,
    }
    for key, value in secret_inputs.items():
        if value is None or key not in SECRET_FIELDS:
            continue
        repository.set_value(key, value, encrypted=True)
        setattr(settings, key, value)
        changed.append(key)
    if changed:
        repository.audit(account.id, "settings.updated", changed)
        session.commit()
    config = await read_config(settings, restart_required_fields=restart)
    return SaveResult(config=config, changed_keys=changed, restart_required_fields=restart)


async def test_agent(settings: Settings, payload: AiConnectionInput) -> ConnectionTestResult:
    if not payload.model.strip():
        return ConnectionTestResult(ok=False, message="请先填写模型名称")
    headers = {"Accept": "application/json"}
    api_key = settings.agent_api_key if payload.api_key is None else payload.api_key
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        async with httpx.AsyncClient(timeout=settings.agent_timeout_seconds) as client:
            response = await client.get(
                f"{payload.base_url.rstrip('/')}/models", headers=headers
            )
            response.raise_for_status()
        return ConnectionTestResult(ok=True, message="模型服务连接正常")
    except httpx.HTTPStatusError as exc:
        return ConnectionTestResult(
            ok=False, message=f"模型服务返回 HTTP {exc.response.status_code}"
        )
    except httpx.HTTPError as exc:
        return ConnectionTestResult(ok=False, message=f"无法连接模型服务：{exc}")


async def test_x_session(settings: Settings) -> ConnectionTestResult:
    try:
        body = await XBrowserCollector(settings).check_session()
        if body.get("valid"):
            return ConnectionTestResult(ok=True, message="X 登录会话有效，可以读取作者动态")
        return ConnectionTestResult(ok=False, message=str(body.get("detail") or "X 会话无效"))
    except RuntimeError as exc:
        return ConnectionTestResult(ok=False, message=str(exc))


async def test_qq(settings: Settings, payload: QqConnectionInput) -> ConnectionTestResult:
    client_secret = (
        settings.qq_bot_client_secret
        if payload.client_secret is None
        else payload.client_secret
    )
    test_settings = settings.model_copy(
        update={
            "qq_bot_enabled": True,
            "qq_bot_app_id": payload.app_id.strip(),
            "qq_bot_client_secret": client_secret,
            "qq_bot_user_openid": payload.user_openid.strip(),
        }
    )
    if not test_settings.qq_bot_configured:
        return ConnectionTestResult(ok=False, message="QQ Bot 凭证尚未完整配置")
    try:
        await QqBotClient(test_settings).send_text("MangaFinder 设置页连接测试成功。")
        return ConnectionTestResult(ok=True, message="测试消息已发送")
    except RuntimeError as exc:
        return ConnectionTestResult(ok=False, message=str(exc))


def _cookie_pairs(header: str) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for part in header.split(";"):
        name, separator, value = part.strip().partition("=")
        if separator and name and value:
            result.append((name, value))
    return result


def _normalized_state(payload: XSessionImport) -> dict[str, Any]:
    if payload.cookie_header:
        cookies = [
            {
                "name": name,
                "value": value,
                "domain": ".x.com",
                "path": "/",
                "expires": -1,
                "httpOnly": name == "auth_token",
                "secure": True,
                "sameSite": "None",
            }
            for name, value in _cookie_pairs(payload.cookie_header)
        ]
    else:
        cookies = []
        for item in (payload.storage_state or {}).get("cookies", []):
            if not isinstance(item, dict) or not item.get("name") or "value" not in item:
                continue
            domain = str(item.get("domain") or "")
            if domain not in {"x.com", ".x.com", "twitter.com", ".twitter.com"}:
                continue
            cookies.append(
                {
                    "name": str(item["name"]),
                    "value": str(item["value"]),
                    "domain": ".x.com",
                    "path": "/",
                    "expires": item.get("expires", -1),
                    "httpOnly": bool(item.get("httpOnly", item["name"] == "auth_token")),
                    "secure": True,
                    "sameSite": item.get("sameSite", "None"),
                }
            )
    names = {item["name"] for item in cookies}
    missing = {"auth_token", "ct0"} - names
    if missing:
        raise ValueError("X 会话缺少必要 Cookie：" + "、".join(sorted(missing)))
    return {"cookies": cookies, "origins": []}


async def import_x_session(
    session: Session,
    settings: Settings,
    account: AdminAccount,
    payload: XSessionImport,
) -> ConnectionTestResult:
    try:
        state = _normalized_state(payload)
    except ValueError as exc:
        return ConnectionTestResult(ok=False, message=str(exc))
    directory = settings.x_session_dir
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    output = directory / "storage-state.json"
    temporary = output.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(output)
    (directory / "runtime-state.json").unlink(missing_ok=True)
    repository = SystemSettingRepository(session, settings)
    repository.audit(account.id, "x_session.replaced", ["x_session"])
    session.commit()
    try:
        await XBrowserCollector(settings).reload_session()
    except RuntimeError as exc:
        return ConnectionTestResult(
            ok=False,
            message=f"会话已安全保存，但采集器尚未重新载入：{exc}",
        )
    return await test_x_session(settings)


async def clear_x_session(
    session: Session, settings: Settings, account: AdminAccount
) -> ConnectionTestResult:
    for name in ("storage-state.json", "runtime-state.json"):
        (settings.x_session_dir / name).unlink(missing_ok=True)
    repository = SystemSettingRepository(session, settings)
    repository.audit(account.id, "x_session.cleared", ["x_session"])
    session.commit()
    try:
        await XBrowserCollector(settings).reload_session()
    except RuntimeError:
        pass
    return ConnectionTestResult(ok=True, message="X 登录会话已清除")

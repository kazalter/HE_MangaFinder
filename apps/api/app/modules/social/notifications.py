from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import NotificationOutbox


class QqBotClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def send_text(self, content: str) -> None:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            token_response = await client.post(
                self.settings.qq_bot_token_url,
                json={
                    "appId": self.settings.qq_bot_app_id,
                    "clientSecret": self.settings.qq_bot_client_secret,
                },
            )
            token_response.raise_for_status()
            access_token = token_response.json()["access_token"]
            response = await client.post(
                f"{self.settings.qq_bot_api_base_url.rstrip('/')}/v2/users/"
                f"{self.settings.qq_bot_user_openid}/messages",
                headers={
                    "Authorization": f"QQBot {access_token}",
                    "X-Union-Appid": self.settings.qq_bot_app_id,
                },
                json={"content": content[:1900], "msg_type": 0},
            )
            response.raise_for_status()


class NotificationService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    async def deliver_pending(self, limit: int = 20) -> dict[str, int]:
        now = datetime.now(UTC)
        rows = list(
            self.session.scalars(
                select(NotificationOutbox)
                .where(
                    NotificationOutbox.status == "pending",
                    or_(
                        NotificationOutbox.next_attempt_at.is_(None),
                        NotificationOutbox.next_attempt_at <= now,
                    ),
                )
                .order_by(NotificationOutbox.created_at)
                .limit(limit)
            )
        )
        delivered = 0
        failed = 0
        for item in rows:
            item.attempts += 1
            if item.channel != "qq":
                item.status = "failed"
                item.error = f"未知通知渠道：{item.channel}"
                failed += 1
                continue
            if not self.settings.qq_bot_configured:
                item.error = "QQ Bot 尚未配置，通知保留在站内"
                item.next_attempt_at = now + timedelta(hours=6)
                failed += 1
                continue
            try:
                await QqBotClient(self.settings).send_text(str(item.payload.get("text", "")))
                item.status = "delivered"
                item.delivered_at = now
                item.error = None
                delivered += 1
            except Exception as exc:
                item.error = str(exc)[:2000]
                item.next_attempt_at = now + timedelta(
                    minutes=min(360, 2 ** min(item.attempts, 8))
                )
                failed += 1
        self.session.commit()
        return {"processed": len(rows), "delivered": delivered, "failed": failed}


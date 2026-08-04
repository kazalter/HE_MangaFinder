import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import DailyDigestDelivery, NotificationOutbox, ReleaseSignal


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


class WebhookClient:
    """Deliver signed JSON without exposing the shared secret in the request body."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def send(
        self,
        *,
        event: str,
        idempotency_key: str,
        payload: dict[str, object],
    ) -> None:
        timestamp = str(int(datetime.now(UTC).timestamp()))
        body = json.dumps(
            {
                "event": event,
                "idempotency_key": idempotency_key,
                "sent_at": datetime.now(UTC).isoformat(),
                "data": payload,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        signature = hmac.new(
            self.settings.webhook_secret.encode("utf-8"),
            timestamp.encode("ascii") + b"." + body,
            hashlib.sha256,
        ).hexdigest()
        try:
            async with httpx.AsyncClient(
                timeout=self.settings.webhook_timeout_seconds,
                follow_redirects=False,
            ) as client:
                response = await client.post(
                    self.settings.webhook_url,
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": self.settings.user_agent,
                        "Idempotency-Key": idempotency_key,
                        "X-MangaFinder-Event": event,
                        "X-MangaFinder-Timestamp": timestamp,
                        "X-MangaFinder-Signature": f"sha256={signature}",
                    },
                )
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"Webhook 返回 HTTP {exc.response.status_code}"
            ) from exc
        except httpx.HTTPError as exc:
            raise RuntimeError("Webhook 连接失败") from exc


class NotificationService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    def _enabled_channels(self) -> list[str]:
        channels: list[str] = []
        if self.settings.qq_bot_enabled:
            channels.append("qq")
        if self.settings.webhook_enabled:
            channels.append("webhook")
        return channels

    @staticmethod
    def _retry_at(now: datetime, attempts: int) -> datetime:
        return now + timedelta(minutes=min(360, 2 ** min(attempts, 8)))

    async def _send(
        self,
        channel: str,
        *,
        event: str,
        idempotency_key: str,
        payload: dict[str, object],
    ) -> None:
        if channel == "qq":
            if not self.settings.qq_bot_configured:
                raise RuntimeError("QQ Bot 尚未完整配置")
            await QqBotClient(self.settings).send_text(str(payload.get("text", "")))
            return
        if channel == "webhook":
            if not self.settings.webhook_configured:
                raise RuntimeError("Webhook 尚未完整配置")
            await WebhookClient(self.settings).send(
                event=event,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            return
        raise ValueError(f"未知通知渠道：{channel}")

    async def deliver_pending(self, limit: int = 20) -> dict[str, int]:
        now = datetime.now(UTC)
        signal_rows = list(
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
        digest_rows = list(
            self.session.scalars(
                select(DailyDigestDelivery)
                .where(
                    DailyDigestDelivery.status == "pending",
                    or_(
                        DailyDigestDelivery.next_attempt_at.is_(None),
                        DailyDigestDelivery.next_attempt_at <= now,
                    ),
                )
                .order_by(DailyDigestDelivery.created_at)
                .limit(max(0, limit - len(signal_rows)))
            )
        )
        delivered = 0
        failed = 0
        for item in signal_rows:
            item.attempts += 1
            try:
                await self._send(
                    item.channel,
                    event=item.event,
                    idempotency_key=item.idempotency_key,
                    payload=item.payload,
                )
            except ValueError as exc:
                item.status = "failed"
                item.error = str(exc)[:2000]
                failed += 1
            except Exception as exc:
                item.error = str(exc)[:2000]
                item.next_attempt_at = self._retry_at(now, item.attempts)
                failed += 1
            else:
                item.status = "delivered"
                item.delivered_at = now
                item.next_attempt_at = None
                item.error = None
                signal = self.session.get(ReleaseSignal, item.signal_id)
                if signal is not None:
                    signal.notified_at = now
                delivered += 1

        channels = self._enabled_channels()
        for item in digest_rows:
            item.attempts += 1
            if not channels:
                item.error = "尚未启用外部通知渠道，日报保留在站内"
                item.next_attempt_at = now + timedelta(hours=6)
                failed += 1
                continue

            delivered_channels = {
                str(channel)
                for channel in item.payload.get("_delivered_channels", [])
            }
            public_payload = {
                str(key): value
                for key, value in item.payload.items()
                if not str(key).startswith("_")
            }
            errors: list[str] = []
            for channel in channels:
                if channel in delivered_channels:
                    continue
                try:
                    await self._send(
                        channel,
                        event="daily_digest",
                        idempotency_key=(
                            f"{channel}:daily_digest:{item.local_date}:{item.content_hash}"
                        ),
                        payload=public_payload,
                    )
                except Exception as exc:
                    errors.append(f"{channel}: {str(exc)[:1000]}")
                else:
                    delivered_channels.add(channel)

            item.payload = {
                **public_payload,
                "_delivered_channels": sorted(delivered_channels),
            }
            if set(channels).issubset(delivered_channels):
                item.status = "delivered"
                item.delivered_at = now
                item.next_attempt_at = None
                item.error = None
                delivered += 1
            else:
                item.error = "; ".join(errors)[:2000] or "部分通知渠道尚未送达"
                item.next_attempt_at = self._retry_at(now, item.attempts)
                failed += 1

        self.session.commit()
        return {
            "processed": len(signal_rows) + len(digest_rows),
            "signal_processed": len(signal_rows),
            "digest_processed": len(digest_rows),
            "delivered": delivered,
            "failed": failed,
        }

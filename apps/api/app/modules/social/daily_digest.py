from datetime import UTC, datetime, timedelta
from hashlib import sha256
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.time import as_utc
from app.db.models import (
    ActivityItem,
    Author,
    DailyDigestDelivery,
    ReleaseSignal,
    ReleaseSignalPost,
    SocialAccount,
    SocialPost,
)
from app.modules.social.digest import DigestService, localize_known_terms

IMPORTANCE_RANK = {"low": 0, "normal": 1, "high": 2, "critical": 3}


class DailyDigestService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    @property
    def timezone(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.settings.social_daily_digest_timezone)
        except ZoneInfoNotFoundError:
            return ZoneInfo("Asia/Shanghai")

    def local_date(self, now: datetime) -> str:
        return as_utc(now).astimezone(self.timezone).date().isoformat()

    def is_due(self, now: datetime) -> bool:
        if not self.settings.social_daily_digest_enabled:
            return False
        local_now = as_utc(now).astimezone(self.timezone)
        hour = min(23, max(0, self.settings.social_daily_digest_hour))
        if local_now.hour < hour:
            return False
        return (
            self.session.scalar(
                select(DailyDigestDelivery.id).where(
                    DailyDigestDelivery.local_date == local_now.date().isoformat()
                )
            )
            is None
        )

    async def build(self, *, force: bool = False, now: datetime | None = None) -> dict[str, object]:
        period_end = as_utc(now or datetime.now(UTC))
        local_date = self.local_date(period_end)
        existing = self.session.scalar(
            select(DailyDigestDelivery).where(
                DailyDigestDelivery.local_date == local_date
            )
        )
        if existing and existing.status != "skipped":
            return {
                "delivery_id": existing.id,
                "delivery_status": existing.status,
                "authors": len(existing.author_ids),
                "created": False,
            }

        previous = self.session.scalar(
            select(DailyDigestDelivery)
            .where(
                DailyDigestDelivery.local_date != local_date,
                DailyDigestDelivery.status.in_(["delivered", "skipped"]),
            )
            .order_by(DailyDigestDelivery.period_end.desc())
            .limit(1)
        )
        if previous:
            period_start = as_utc(previous.period_end)
        else:
            # The first delivery establishes a useful baseline instead of silently
            # ignoring activities that predate the day the feature was enabled.
            lookback_days = min(
                30,
                max(1, self.settings.social_daily_digest_initial_lookback_days),
            )
            period_start = period_end - timedelta(days=lookback_days)

        author_activities = self._recent_activities(period_start, period_end)
        sections: list[tuple[int, str, list[str]]] = []
        for author_id, activities in author_activities.items():
            digest = await DigestService(self.session, self.settings).refresh(author_id)
            if not digest:
                continue
            recent_post_ids = self._recent_post_ids(author_id, period_start, period_end)
            notified_post_ids = self._notified_post_ids(author_id, period_start, period_end)
            highlights: list[str] = []
            minimum = IMPORTANCE_RANK.get(
                self.settings.social_daily_digest_min_importance, 1
            )
            for item in digest.highlights:
                post_ids = {int(value) for value in item.get("post_ids", [])}
                if not post_ids.intersection(recent_post_ids):
                    continue
                if IMPORTANCE_RANK.get(str(item.get("importance")), 1) < minimum:
                    continue
                text = localize_known_terms(str(item.get("text", "")).strip())
                if not text:
                    continue
                if post_ids.intersection(notified_post_ids):
                    text = f"{text.rstrip('。')}（已即时通知）"
                highlights.append(text)
                if len(highlights) >= self.settings.social_daily_digest_max_items_per_author:
                    break
            if not highlights:
                highlights = self._activity_fallback(activities)
            if highlights:
                author = self.session.get(Author, author_id)
                sections.append((author_id, author.name if author else "已删除作者", highlights))
            if len(sections) >= self.settings.social_daily_digest_max_authors:
                break

        if not sections:
            if force and existing is None:
                return {
                    "delivery_id": None,
                    "delivery_status": "empty",
                    "authors": 0,
                    "created": False,
                }
            delivery = existing or DailyDigestDelivery(
                local_date=local_date,
                timezone=self.timezone.key,
                period_start=period_start,
                period_end=period_end,
                author_ids=[],
                content_hash=sha256(f"empty:{local_date}".encode()).hexdigest(),
                payload={},
                status="skipped",
            )
            if existing is None:
                self.session.add(delivery)
            else:
                delivery.period_start = period_start
                delivery.period_end = period_end
            self.session.flush()
            return {
                "delivery_id": delivery.id,
                "delivery_status": "skipped",
                "authors": 0,
                "created": existing is None,
            }

        message = self._message(local_date, sections)
        content_hash = sha256(message.encode("utf-8")).hexdigest()
        delivery = existing or DailyDigestDelivery(
            local_date=local_date,
            timezone=self.timezone.key,
            period_start=period_start,
            period_end=period_end,
            author_ids=[item[0] for item in sections],
            content_hash=content_hash,
            payload={"text": message},
            status="pending",
            next_attempt_at=period_end,
        )
        if existing is None:
            self.session.add(delivery)
        else:
            delivery.timezone = self.timezone.key
            delivery.period_start = period_start
            delivery.period_end = period_end
            delivery.author_ids = [item[0] for item in sections]
            delivery.content_hash = content_hash
            delivery.payload = {"text": message}
            delivery.status = "pending"
            delivery.next_attempt_at = period_end
            delivery.error = None
        self.session.flush()
        return {
            "delivery_id": delivery.id,
            "delivery_status": delivery.status,
            "authors": len(sections),
            "created": True,
        }

    def _recent_activities(
        self, period_start: datetime, period_end: datetime
    ) -> dict[int, list[ActivityItem]]:
        minimum = IMPORTANCE_RANK.get(self.settings.social_daily_digest_min_importance, 1)
        allowed = [
            value for value, rank in IMPORTANCE_RANK.items() if rank >= minimum
        ]
        rows = list(
            self.session.scalars(
                select(ActivityItem)
                .where(
                    ActivityItem.ended_at > period_start,
                    ActivityItem.ended_at <= period_end,
                    ActivityItem.importance.in_(allowed),
                )
                .order_by(ActivityItem.ended_at.desc(), ActivityItem.id.desc())
            )
        )
        grouped: dict[int, list[ActivityItem]] = {}
        for item in rows:
            grouped.setdefault(item.author_id, []).append(item)
        return grouped

    def _recent_post_ids(
        self, author_id: int, period_start: datetime, period_end: datetime
    ) -> set[int]:
        return set(
            self.session.scalars(
                select(SocialPost.id)
                .join(SocialAccount, SocialAccount.id == SocialPost.account_id)
                .where(
                    SocialAccount.author_id == author_id,
                    SocialPost.post_type != "retweet",
                    SocialPost.posted_at > period_start,
                    SocialPost.posted_at <= period_end,
                )
            )
        )

    def _notified_post_ids(
        self, author_id: int, period_start: datetime, period_end: datetime
    ) -> set[int]:
        return set(
            self.session.scalars(
                select(ReleaseSignalPost.post_id)
                .join(ReleaseSignal, ReleaseSignal.id == ReleaseSignalPost.signal_id)
                .where(
                    ReleaseSignal.author_id == author_id,
                    ReleaseSignal.notified_at.is_not(None),
                    ReleaseSignal.notified_at > period_start,
                    ReleaseSignal.notified_at <= period_end,
                )
            )
        )

    def _activity_fallback(self, activities: list[ActivityItem]) -> list[str]:
        result: list[str] = []
        for item in activities:
            text = localize_known_terms((item.summary or item.headline).strip())
            if text and text not in result:
                result.append(text)
            if len(result) >= self.settings.social_daily_digest_max_items_per_author:
                break
        return result

    def _message(
        self, local_date: str, sections: list[tuple[int, str, list[str]]]
    ) -> str:
        lines = ["【MangaFinder · 作者近况日报】", f"日期：{local_date}"]
        item_count = 0
        for _, author_name, highlights in sections:
            lines.extend(["", author_name])
            for item in highlights:
                candidate = f"- {item.rstrip('。')}"
                if len("\n".join([*lines, candidate])) > 1700:
                    break
                lines.append(candidate)
                item_count += 1
        lines.extend(
            [
                "",
                f"共汇总 {len(sections)} 位作者、{item_count} 项近况；纯转推已排除。",
                f"详情：{self.settings.public_base_url.rstrip('/')}/?view=social",
            ]
        )
        return "\n".join(lines)[:1900]

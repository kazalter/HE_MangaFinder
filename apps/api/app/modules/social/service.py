import json
import random
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.time import as_utc, utc_timestamp
from app.db.models import (
    AuthorWork,
    ReleaseSignal,
    SocialAccount,
    SocialAgentReview,
    SocialPost,
    Work,
)
from app.modules.social.activity import ActivityService
from app.modules.social.agent import SocialReleaseReviewer
from app.modules.social.collector import CollectorPost, XBrowserCollector
from app.modules.social.digest import DigestService
from app.modules.social.ocr import LocalMediaOcr
from app.modules.social.repository import SocialRepository
from app.modules.social.rules import RuleAssessment, assess_post, cluster_key


class SocialSyncService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.repository = SocialRepository(session)

    async def sync_account(self, account_id: int) -> dict[str, int]:
        account = self.repository.get_account(account_id)
        if not account:
            raise ValueError("社交账号不存在")
        if account.status != "confirmed":
            raise ValueError("必须先人工确认账号才能同步")
        try:
            collector = XBrowserCollector(self.settings)
            profile = None
            if not account.avatar_url:
                try:
                    profile = await collector.profile(account.handle)
                except RuntimeError:
                    # A missing avatar must not prevent post collection. Profile metadata
                    # will be retried on the next sync while the account has no avatar.
                    pass
            if profile:
                account.platform_user_id = profile.id or account.platform_user_id
                account.display_name = profile.display_name or account.display_name
                account.profile_url = profile.profile_url or account.profile_url
                account.avatar_url = profile.avatar_url or account.avatar_url
            incoming = await collector.posts(
                account.handle,
                account.last_post_id,
                self.settings.social_max_posts_per_sync,
            )
            result = await self.ingest(account, incoming)
            now = datetime.now(UTC)
            account.last_synced_at = now
            account.sync_error = None
            if incoming:
                newest = max(incoming, key=lambda item: utc_timestamp(item.posted_at))
                account.last_post_id = newest.id
            interval = self._next_interval(account.author_id)
            jitter = random.uniform(-0.08, 0.08)  # noqa: S311 - scheduling jitter only
            account.next_sync_at = now + timedelta(minutes=interval * (1 + jitter))
            self.session.commit()
            return result
        except Exception as exc:
            self.session.rollback()
            account = self.repository.get_account(account_id)
            if account is None:
                raise
            account.sync_error = str(exc)[:2000]
            account.next_sync_at = datetime.now(UTC) + timedelta(hours=6)
            self.session.commit()
            raise

    async def ingest(
        self, account: SocialAccount, incoming: list[CollectorPost]
    ) -> dict[str, int]:
        cutoff = datetime.now(UTC) - timedelta(days=self.settings.social_initial_backfill_days)
        first_sync = account.last_post_id is None
        created_count = 0
        analyzed_count = 0
        signal_count = 0
        activity_count = 0
        # First sync may return a large media-heavy timeline. Persist every post, but spend
        # OCR time on the newest items; later incremental runs naturally process all new media.
        newest_media_post_ids = [
            item.id
            for item in sorted(
                incoming, key=lambda value: utc_timestamp(value.posted_at), reverse=True
            )
            if item.media
        ]
        ocr_post_ids = set(
            newest_media_post_ids[: max(0, self.settings.social_ocr_max_posts_per_sync)]
        )
        for item in sorted(incoming, key=lambda value: utc_timestamp(value.posted_at)):
            posted_at = as_utc(item.posted_at)
            if first_sync and posted_at < cutoff:
                continue
            content_hash = sha256(
                json.dumps(item.model_dump(mode="json"), sort_keys=True).encode("utf-8")
            ).hexdigest()
            post, created, changed = self.repository.upsert_post(
                account.id,
                {
                    "platform_post_id": item.id,
                    "post_type": item.post_type,
                    "text": item.text,
                    "url": item.url,
                    "conversation_id": item.conversation_id,
                    "replied_to_post_id": item.replied_to_post_id,
                    "quoted_post_id": item.quoted_post_id,
                    "media": item.media,
                    "links": item.links,
                    "raw_metadata": item.raw,
                    "content_hash": content_hash,
                    "posted_at": posted_at,
                },
            )
            if created:
                created_count += 1
            if not changed and self.repository.signal_for_post(post.id):
                continue
            if post.post_type == "retweet":
                continue
            if post.media and not post.ocr_text and item.id in ocr_post_ids:
                post.ocr_text = await LocalMediaOcr(self.settings).extract(post.media)
            analyzed_count += 1
            if ActivityService(self.session).ingest(account, post):
                activity_count += 1
            if await self.analyze_post(account, post):
                signal_count += 1
        if activity_count:
            await DigestService(self.session, self.settings).refresh(account.author_id)
        self.session.flush()
        return {
            "fetched": len(incoming),
            "created": created_count,
            "analyzed": analyzed_count,
            "signals": signal_count,
            "activities": activity_count,
        }

    async def analyze_post(self, account: SocialAccount, post: SocialPost) -> ReleaseSignal | None:
        assessment = assess_post(post)
        if not assessment.candidate:
            return None
        snapshot = self._evidence_snapshot(account, post, assessment)
        verdict = None
        review_error = None
        if self.settings.social_agent_configured:
            try:
                verdict = (await SocialReleaseReviewer(self.settings).review(snapshot)).verdict
            except Exception as exc:
                review_error = str(exc)

        kind = verdict.kind if verdict else assessment.kind
        title = verdict.title if verdict and verdict.title else assessment.title
        event_code = (
            verdict.event_code if verdict and verdict.event_code else assessment.event_code
        )
        booth = verdict.booth if verdict and verdict.booth else assessment.booth
        confidence = assessment.confidence
        if verdict:
            confidence = min(verdict.confidence, max(assessment.confidence + 0.12, 0.60))
        grounded_evidence = list(assessment.evidence)
        grounded_counter = list(assessment.counter_evidence)
        if verdict:
            grounded_evidence.extend(self._ground(verdict.evidence, snapshot))
            grounded_counter.extend(self._ground(verdict.counter_evidence, snapshot))
        stores = list(
            dict.fromkeys(
                assessment.store_urls + (verdict.store_urls if verdict else [])
            )
        )

        status = "archived" if confidence < self.settings.social_candidate_threshold else "pending"
        model_accepts_new = (
            verdict.has_new_work
            if verdict
            else assessment.explicit_new_work and not self.settings.social_agent_configured
        )
        safe_new_release = (
            kind == "new_release"
            and assessment.explicit_new_work
            and assessment.independent_signals >= 2
            and model_accepts_new
        )
        safe_non_new = kind in {
            "event_participation",
            "reprint",
            "delay",
            "cancellation",
        } and bool(event_code or assessment.counter_evidence)
        if (
            confidence >= self.settings.social_auto_confirm_threshold
            and (safe_new_release or safe_non_new)
        ):
            status = "confirmed"

        key_assessment = RuleAssessment(
            candidate=True,
            kind=kind,
            confidence=confidence,
            title=title,
            event_code=event_code,
            booth=booth,
            store_urls=stores,
        )
        key = cluster_key(account.author_id, key_assessment, post)
        signal = self.repository.signal_for_post(post.id) or self.repository.signal_by_cluster(
            account.author_id, key
        )
        is_new = signal is None
        if signal is None:
            signal = ReleaseSignal(
                author_id=account.author_id,
                primary_post_id=post.id,
                cluster_key=key,
                kind=kind,
                title=title,
                event_code=event_code,
                booth=booth,
                store_urls=stores,
                confidence=confidence,
                status=status,
                evidence=grounded_evidence,
                counter_evidence=grounded_counter,
                missing_information=verdict.missing_information if verdict else [],
            )
            self.session.add(signal)
            self.session.flush()
        elif confidence >= signal.confidence or kind in {"delay", "cancellation"}:
            signal.kind = kind
            signal.title = title or signal.title
            signal.event_code = event_code or signal.event_code
            signal.booth = booth or signal.booth
            signal.store_urls = list(dict.fromkeys(signal.store_urls + stores))
            signal.confidence = max(signal.confidence, confidence)
            if signal.status not in {"rejected", "released"}:
                signal.status = status
            signal.evidence = list(dict.fromkeys(signal.evidence + grounded_evidence))
            signal.counter_evidence = list(
                dict.fromkeys(signal.counter_evidence + grounded_counter)
            )
        self.repository.attach_post(signal, post)

        if self.settings.social_agent_configured:
            self.session.add(
                SocialAgentReview(
                    signal_id=signal.id,
                    post_id=post.id,
                    evidence_hash=sha256(
                        json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode()
                    ).hexdigest(),
                    model=self.settings.agent_model,
                    prompt_version=self.settings.social_agent_prompt_version,
                    status="failed" if review_error else "succeeded",
                    verdict=verdict.model_dump(mode="json") if verdict else {},
                    input_snapshot=snapshot,
                    error=review_error,
                )
            )
        if signal.status == "confirmed" and (is_new or signal.notified_at is None):
            self.queue_notification(signal, account, post)
        self.session.flush()
        return signal

    def _evidence_snapshot(
        self, account: SocialAccount, post: SocialPost, assessment: RuleAssessment
    ) -> dict[str, Any]:
        author = self.repository.author(account.author_id)
        thread_posts: list[dict[str, str]] = []
        if post.conversation_id:
            context_rows = list(
                self.session.scalars(
                    select(SocialPost)
                    .where(
                        SocialPost.account_id == account.id,
                        SocialPost.conversation_id == post.conversation_id,
                        SocialPost.id != post.id,
                    )
                    .order_by(SocialPost.posted_at)
                    .limit(5)
                )
            )
            thread_posts = [
                {
                    "post_type": item.post_type,
                    "text": item.text,
                    "ocr_text": item.ocr_text or "",
                    "url": item.url,
                }
                for item in context_rows
            ]
        recent_titles = list(
            self.session.scalars(
                select(Work.title)
                .join(AuthorWork, AuthorWork.work_id == Work.id)
                .where(AuthorWork.author_id == account.author_id)
                .order_by(Work.updated_at.desc())
                .limit(20)
            )
        )
        return {
            "tracked_author": author.name if author else "",
            "account": {
                "handle": account.handle,
                "display_name": account.display_name,
                "account_type": account.account_type,
                "human_confirmed": account.status == "confirmed",
            },
            "post": {
                "type": post.post_type,
                "text": post.text,
                "ocr_text": post.ocr_text,
                "posted_at": post.posted_at.isoformat(),
                "links": post.links,
                "media_count": len(post.media),
                "conversation_id": post.conversation_id,
                "quoted_context": post.raw_metadata.get("quoted_text"),
                "reply_context": post.raw_metadata.get("reply_context"),
                "self_thread_context": thread_posts,
            },
            "rule_observations": {
                "kind": assessment.kind,
                "title": assessment.title,
                "event_code": assessment.event_code,
                "booth": assessment.booth,
                "store_urls": assessment.store_urls,
                "evidence": assessment.evidence,
                "counter_evidence": assessment.counter_evidence,
            },
            "recent_catalog_titles": recent_titles,
        }

    @staticmethod
    def _ground(items: list[str], snapshot: dict[str, Any]) -> list[str]:
        haystack = json.dumps(snapshot, ensure_ascii=False).casefold()
        return [item for item in items if item and item.casefold() in haystack]

    def queue_notification(
        self, signal: ReleaseSignal, account: SocialAccount, post: SocialPost
    ) -> None:
        author = self.repository.author(account.author_id)
        label = {
            "new_release": "新作预告",
            "event_participation": "参展动态",
            "preorder": "预售动态",
            "delay": "延期",
            "cancellation": "取消",
            "reprint": "再版/旧刊",
        }.get(signal.kind, signal.kind)
        title = signal.title or "标题尚未确认"
        evidence = "；".join(signal.evidence[:3]) or "等待补充证据"
        text = (
            f"【MangaFinder · {label}】\n"
            f"作者：{author.name if author else account.handle}\n"
            f"作品：{title}\n"
            f"展会：{signal.event_code or '-'}  摊位：{signal.booth or '-'}\n"
            f"置信度：{signal.confidence:.0%}\n"
            f"依据：{evidence}\n"
            f"原帖：{post.url}\n"
            f"审核：{self.settings.public_base_url.rstrip('/')}/?radar={signal.id}"
        )
        self.repository.enqueue_notification(signal, {"text": text})

    def _next_interval(self, author_id: int) -> int:
        recent = datetime.now(UTC) - timedelta(days=45)
        active = self.session.scalar(
            select(ReleaseSignal.id).where(
                ReleaseSignal.author_id == author_id,
                ReleaseSignal.event_code.is_not(None),
                ReleaseSignal.updated_at >= recent,
                ReleaseSignal.status.in_(["pending", "confirmed"]),
            )
        )
        return (
            self.settings.social_event_sync_interval_minutes
            if active
            else self.settings.social_sync_interval_minutes
        )

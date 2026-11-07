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
    SocialPostProcessing,
    SocialSyncProgress,
    Work,
)
from app.modules.jobs.repository import JobRepository
from app.modules.social.activity import ActivityService, activity_cluster_key, assess_activity
from app.modules.social.agent import SocialReleaseReviewer
from app.modules.social.collector import CollectorPost, XBrowserCollector
from app.modules.social.digest import DigestService
from app.modules.social.media_archive import SocialAvatarArchive, SocialMediaArchive
from app.modules.social.ocr import LocalMediaOcr
from app.modules.social.planned_releases import PlannedReleaseService
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
            avatar_url = (
                profile.avatar_url if profile and profile.avatar_url else account.avatar_url
            )
            if avatar_url:
                await SocialAvatarArchive(self.settings).archive(avatar_url)
            progress = self.session.get(SocialSyncProgress, account.id)
            stale_progress = None
            if progress and (
                progress.handle.casefold() != account.handle.casefold()
                or progress.since_id != account.last_post_id
            ):
                stale_progress = progress
                progress = None
            cutoff = (
                progress.backfill_cutoff
                if progress
                else datetime.now(UTC) - timedelta(days=self.settings.social_initial_backfill_days)
            )
            watermark_time = None
            if account.last_post_id:
                watermark_time = self.session.scalar(
                    select(SocialPost.posted_at).where(
                        SocialPost.account_id == account.id,
                        SocialPost.platform_post_id == account.last_post_id,
                    )
                )
            batch = await collector.posts_batch(
                account.handle,
                account.last_post_id,
                self.settings.social_max_posts_per_sync,
                cursor=progress.cursor if progress else None,
                stop_before=cutoff if account.last_post_id is None else watermark_time,
            )
            incoming = batch.posts
            if stale_progress:
                self.session.delete(stale_progress)
                self.session.flush()
            if profile:
                account.platform_user_id = profile.id or account.platform_user_id
                account.display_name = profile.display_name or account.display_name
                account.profile_url = profile.profile_url or account.profile_url
                account.avatar_url = profile.avatar_url or account.avatar_url
            result = self.queue_posts(account, incoming, backfill_cutoff=cutoff)
            now = datetime.now(UTC)
            account.sync_error = None
            newest = (
                max(incoming, key=lambda item: utc_timestamp(item.posted_at)) if incoming else None
            )
            if (
                newest
                and watermark_time
                and utc_timestamp(newest.posted_at) < utc_timestamp(watermark_time)
            ):
                newest = None
            if batch.complete:
                if (
                    progress
                    and progress.newest_post_id
                    and (
                        newest is None
                        or (
                            progress.newest_posted_at is not None
                            and utc_timestamp(progress.newest_posted_at)
                            >= utc_timestamp(newest.posted_at)
                        )
                    )
                ):
                    account.last_post_id = progress.newest_post_id
                elif newest:
                    account.last_post_id = newest.id
                if progress:
                    self.session.delete(progress)
                account.last_synced_at = now
                interval = self._next_interval(account.author_id)
                jitter = random.uniform(-0.08, 0.08)  # noqa: S311 - scheduling jitter only
                account.next_sync_at = now + timedelta(minutes=interval * (1 + jitter))
            else:
                if progress is None:
                    progress = SocialSyncProgress(
                        account_id=account.id,
                        handle=account.handle,
                        since_id=account.last_post_id,
                        cursor=batch.next_cursor,
                        backfill_cutoff=cutoff if account.last_post_id is None else None,
                    )
                    self.session.add(progress)
                else:
                    progress.cursor = batch.next_cursor
                if newest and (
                    progress.newest_posted_at is None
                    or utc_timestamp(newest.posted_at) > utc_timestamp(progress.newest_posted_at)
                ):
                    progress.newest_post_id = newest.id
                    progress.newest_posted_at = newest.posted_at
                account.next_sync_at = now
            result["complete"] = batch.complete
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

    def queue_posts(
        self,
        account: SocialAccount,
        incoming: list[CollectorPost],
        *,
        backfill_cutoff: datetime | None = None,
    ) -> dict[str, int]:
        """Save raw facts and durable follow-up work in the same short transaction."""
        cutoff = as_utc(
            backfill_cutoff
            or (datetime.now(UTC) - timedelta(days=self.settings.social_initial_backfill_days))
        )
        created_count = queued = 0
        for item in sorted(incoming, key=lambda value: utc_timestamp(value.posted_at)):
            if account.last_post_id is None and as_utc(item.posted_at) < cutoff:
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
                    "posted_at": as_utc(item.posted_at),
                    "availability_status": "available",
                    "availability_reason": None,
                    "unavailable_since": None,
                    "availability_failure_count": 0,
                },
            )
            created_count += int(created)
            if item.post_type == "retweet":
                continue
            state = self.session.get(SocialPostProcessing, post.id)
            if not changed and state and state.status == "done":
                continue
            if state is None:
                state = SocialPostProcessing(post_id=post.id, content_hash=content_hash)
                self.session.add(state)
            elif changed:
                state.content_hash = content_hash
                state.status = "pending"
                state.ocr_status = "pending"
                state.backfill_only = False
                state.last_error = None
            JobRepository(self.session).enqueue_social_post(post.id)
            queued += 1
        return {"fetched": len(incoming), "created": created_count, "queued": queued}

    async def process_post(self, post_id: int) -> dict[str, object]:
        post = self.session.get(SocialPost, post_id)
        state = self.session.get(SocialPostProcessing, post_id)
        if not post or not state:
            return {"status": "missing"}
        account = self.repository.get_account(post.account_id)
        if not account:
            return {"status": "missing"}
        expected = post.content_hash
        old_ocr_text = post.ocr_text
        was_backfill = state.backfill_only
        if state.status == "done" and state.content_hash == expected:
            return {"status": "done"}
        archive = SocialMediaArchive(self.session, self.settings)
        local_media = {}
        if was_backfill:
            # Reuse existing local images but do not expand or evict the archive
            # merely because historical OCR is being filled in.
            for index, item in enumerate(post.media):
                if item.get("type") == "image":
                    asset = archive.asset(post.id, index)
                    path = archive.local_file(asset) if asset else None
                    if path:
                        local_media[index] = path
        elif self.settings.social_media_cache_max_bytes > 0:
            for index, item in enumerate(post.media):
                if item.get("type") != "image" or not item.get("url"):
                    continue
                asset = await archive.archive(post.id, index, str(item["url"]))
                path = archive.local_file(asset)
                if path:
                    local_media[index] = path
                self.session.commit()
            archive.enforce_quota()
            self.session.commit()
        if post.media and state.ocr_status in {"pending", "retry", "disabled"}:
            result = await LocalMediaOcr(self.settings).extract_result(post.media, local_media)
            self.session.refresh(post)
            if post.content_hash != expected:
                raise RuntimeError("帖子在处理时发生变化，将重新处理")
            if result.text:
                post.ocr_text = result.text
            state.ocr_status = result.status
            state.last_error = result.error
            self.session.commit()
        elif not post.media:
            state.ocr_status = "not_applicable"
            self.session.commit()
        elif post.ocr_text:
            state.ocr_status = "text"
            self.session.commit()

        if state.backfill_only and post.ocr_text == old_ocr_text:
            state.status = "done"
            self.session.commit()
            return {"status": "done", "activity": False, "signal": False}

        # Prepare model evidence and await the external reviewer before writing activity.
        prepared = await self._prepare_review(account, post)
        self.session.refresh(post)
        if post.content_hash != expected:
            raise RuntimeError("帖子在分析时发生变化，将重新处理")
        assessment = assess_activity(post)
        existing_activity = (
            self.repository.activity_by_cluster(
                account.author_id,
                activity_cluster_key(account.author_id, post, assessment.category),
            )
            if was_backfill and assessment
            else None
        )
        activity = ActivityService(self.session).ingest(account, post)
        if was_backfill and activity and existing_activity is None:
            activity.is_read = True
        if activity and activity.importance in {"high", "critical"}:
            archive.pin_post(post.id, activity.importance)
        signal = await self.analyze_post(account, post, prepared=prepared, backfill=was_backfill)
        if signal and signal.status in {"pending", "confirmed", "linked"}:
            archive.pin_post(post.id, "critical")
        state.content_hash = expected
        state.status = "done"
        state.backfill_only = False
        if state.ocr_status not in {"retry", "disabled"}:
            state.last_error = None
        if activity:
            JobRepository(self.session).enqueue_social_digest(account.author_id)
        self.session.commit()
        return {"status": "done", "activity": bool(activity), "signal": bool(signal)}

    async def _prepare_review(self, account: SocialAccount, post: SocialPost):
        assessment = assess_post(post)
        if not assessment.candidate:
            return assessment, None, None, None
        snapshot = self._evidence_snapshot(account, post, assessment)
        verdict = None
        review_error = None
        if self.settings.social_agent_configured:
            try:
                verdict = (await SocialReleaseReviewer(self.settings).review(snapshot)).verdict
            except Exception as exc:
                review_error = str(exc)
        return assessment, snapshot, verdict, review_error

    async def analyze_post(
        self,
        account: SocialAccount,
        post: SocialPost,
        *,
        prepared=None,
        backfill: bool = False,
    ) -> ReleaseSignal | None:
        assessment, snapshot, verdict, review_error = (
            prepared if prepared is not None else await self._prepare_review(account, post)
        )
        if not assessment.candidate:
            return None

        kind = verdict.kind if verdict else assessment.kind
        title = verdict.title if verdict and verdict.title else assessment.title
        event_code = verdict.event_code if verdict and verdict.event_code else assessment.event_code
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
            dict.fromkeys(assessment.store_urls + (verdict.store_urls if verdict else []))
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
        if confidence >= self.settings.social_auto_confirm_threshold and (
            safe_new_release or safe_non_new
        ):
            status = "confirmed"

        if backfill and status == "confirmed":
            status = "pending"

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
            if signal.status not in {"rejected", "released", "confirmed", "linked"}:
                signal.status = status
            signal.evidence = list(dict.fromkeys(signal.evidence + grounded_evidence))
            signal.counter_evidence = list(
                dict.fromkeys(signal.counter_evidence + grounded_counter)
            )
        self.repository.attach_post(signal, post)
        PlannedReleaseService(self.session).absorb_signal(signal)

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
        if not backfill and signal.status == "confirmed" and (is_new or signal.notified_at is None):
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
        channels: list[str] = []
        if self.settings.qq_bot_enabled:
            channels.append("qq")
        if self.settings.webhook_enabled:
            channels.append("webhook")
        for channel in channels:
            self.repository.enqueue_notification(signal, channel, {"text": text})

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

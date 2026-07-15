from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db.models import (
    ActivityItem,
    ActivityItemPost,
    Author,
    AuthorDigest,
    NotificationOutbox,
    ReleaseSignal,
    ReleaseSignalPost,
    SocialAccount,
    SocialPost,
)


class SocialRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def accounts_for_author(self, author_id: int) -> list[SocialAccount]:
        return list(
            self.session.scalars(
                select(SocialAccount)
                .where(SocialAccount.author_id == author_id)
                .order_by(SocialAccount.created_at)
            )
        )

    def get_account(self, account_id: int) -> SocialAccount | None:
        return self.session.get(SocialAccount, account_id)

    def add_account(
        self,
        author_id: int,
        handle: str,
        account_type: str,
        confirmed: bool,
        display_name: str | None = None,
        profile_url: str | None = None,
        avatar_url: str | None = None,
        match_score: float | None = None,
        evidence: list[str] | None = None,
    ) -> SocialAccount:
        existing = self.session.scalar(
            select(SocialAccount).where(
                SocialAccount.author_id == author_id,
                SocialAccount.platform == "x",
                SocialAccount.handle == handle.casefold(),
            )
        )
        if existing:
            return existing
        account = SocialAccount(
            author_id=author_id,
            platform="x",
            handle=handle.casefold(),
            display_name=display_name,
            profile_url=profile_url or f"https://x.com/{handle}",
            avatar_url=avatar_url,
            account_type=account_type,
            status="confirmed" if confirmed else "suggested",
            match_score=match_score,
            evidence=evidence or [],
            next_sync_at=datetime.now(UTC) if confirmed else None,
        )
        self.session.add(account)
        self.session.flush()
        return account

    def due_accounts(self, now: datetime) -> list[SocialAccount]:
        return list(
            self.session.scalars(
                select(SocialAccount).where(
                    SocialAccount.status == "confirmed",
                    or_(SocialAccount.next_sync_at.is_(None), SocialAccount.next_sync_at <= now),
                )
            )
        )

    def upsert_post(
        self, account_id: int, values: dict[str, object]
    ) -> tuple[SocialPost, bool, bool]:
        platform_post_id = str(values["platform_post_id"])
        post = self.session.scalar(
            select(SocialPost).where(
                SocialPost.account_id == account_id,
                SocialPost.platform_post_id == platform_post_id,
            )
        )
        created = post is None
        changed = created or post.content_hash != values.get("content_hash") if post else True
        if post is None:
            post = SocialPost(account_id=account_id, **values)
            self.session.add(post)
        else:
            for key, value in values.items():
                setattr(post, key, value)
        self.session.flush()
        return post, created, changed

    def signal_for_post(self, post_id: int) -> ReleaseSignal | None:
        return self.session.scalar(
            select(ReleaseSignal)
            .join(ReleaseSignalPost, ReleaseSignalPost.signal_id == ReleaseSignal.id)
            .where(ReleaseSignalPost.post_id == post_id)
        )

    def signal_by_cluster(self, author_id: int, key: str) -> ReleaseSignal | None:
        return self.session.scalar(
            select(ReleaseSignal).where(
                ReleaseSignal.author_id == author_id, ReleaseSignal.cluster_key == key
            )
        )

    def attach_post(self, signal: ReleaseSignal, post: SocialPost) -> None:
        exists = self.session.get(ReleaseSignalPost, (signal.id, post.id))
        if not exists:
            self.session.add(ReleaseSignalPost(signal_id=signal.id, post_id=post.id))

    def list_signals(
        self, author_id: int | None = None, status: str | None = None, limit: int = 100
    ) -> list[ReleaseSignal]:
        statement = select(ReleaseSignal)
        if author_id is not None:
            statement = statement.where(ReleaseSignal.author_id == author_id)
        if status:
            statement = statement.where(ReleaseSignal.status == status)
        return list(
            self.session.scalars(
                statement.order_by(ReleaseSignal.updated_at.desc()).limit(limit)
            )
        )

    def signal_posts(self, signal_id: int) -> list[SocialPost]:
        return list(
            self.session.scalars(
                select(SocialPost)
                .join(ReleaseSignalPost, ReleaseSignalPost.post_id == SocialPost.id)
                .where(ReleaseSignalPost.signal_id == signal_id)
                .order_by(SocialPost.posted_at)
            )
        )

    def activity_by_cluster(self, author_id: int, key: str) -> ActivityItem | None:
        return self.session.scalar(
            select(ActivityItem).where(
                ActivityItem.author_id == author_id,
                ActivityItem.cluster_key == key,
            )
        )

    def attach_activity_post(self, activity: ActivityItem, post: SocialPost) -> None:
        if not self.session.get(ActivityItemPost, (activity.id, post.id)):
            self.session.add(ActivityItemPost(activity_id=activity.id, post_id=post.id))

    def activity_posts(self, activity_id: int) -> list[SocialPost]:
        return list(
            self.session.scalars(
                select(SocialPost)
                .join(ActivityItemPost, ActivityItemPost.post_id == SocialPost.id)
                .where(ActivityItemPost.activity_id == activity_id)
                .order_by(SocialPost.posted_at)
            )
        )

    def list_activities(
        self,
        author_id: int | None = None,
        category: str | None = None,
        limit: int = 100,
    ) -> list[ActivityItem]:
        statement = select(ActivityItem)
        if author_id is not None:
            statement = statement.where(ActivityItem.author_id == author_id)
        if category:
            statement = statement.where(ActivityItem.category == category)
        return list(
            self.session.scalars(
                statement.order_by(ActivityItem.ended_at.desc(), ActivityItem.id.desc()).limit(
                    limit
                )
            )
        )

    def recent_posts_for_author(self, author_id: int, since: datetime) -> list[SocialPost]:
        return list(
            self.session.scalars(
                select(SocialPost)
                .join(SocialAccount, SocialAccount.id == SocialPost.account_id)
                .where(
                    SocialAccount.author_id == author_id,
                    SocialPost.posted_at >= since,
                    SocialPost.post_type != "retweet",
                )
                .order_by(SocialPost.posted_at)
            )
        )

    def digest_for_period(
        self, author_id: int, period_type: str, period_end: datetime
    ) -> AuthorDigest | None:
        return self.session.scalar(
            select(AuthorDigest).where(
                AuthorDigest.author_id == author_id,
                AuthorDigest.period_type == period_type,
                AuthorDigest.period_end == period_end,
            )
        )

    def latest_digest(
        self, author_id: int, period_type: str = "rolling_7d"
    ) -> AuthorDigest | None:
        return self.session.scalar(
            select(AuthorDigest)
            .where(
                AuthorDigest.author_id == author_id,
                AuthorDigest.period_type == period_type,
            )
            .order_by(AuthorDigest.period_end.desc(), AuthorDigest.id.desc())
            .limit(1)
        )

    def count_signals(self, status: str | None = None) -> int:
        statement = select(ReleaseSignal.id)
        if status:
            statement = statement.where(ReleaseSignal.status == status)
        return len(list(self.session.scalars(statement)))

    def author(self, author_id: int) -> Author | None:
        return self.session.get(Author, author_id)

    def enqueue_notification(self, signal: ReleaseSignal, payload: dict[str, object]) -> None:
        key = f"qq:signal:{signal.id}:{signal.kind}:{signal.status}"
        exists = self.session.scalar(
            select(NotificationOutbox).where(NotificationOutbox.idempotency_key == key)
        )
        if not exists:
            self.session.add(
                NotificationOutbox(
                    signal_id=signal.id,
                    channel="qq",
                    idempotency_key=key,
                    payload=payload,
                    next_attempt_at=datetime.now(UTC),
                )
            )

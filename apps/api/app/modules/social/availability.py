from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import SocialAccount, SocialPost
from app.modules.social.collector import XBrowserCollector


class PostAvailabilityService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    async def verify(self, post: SocialPost) -> SocialPost:
        account = self.session.get(SocialAccount, post.account_id)
        if account is None:
            raise ValueError("帖子绑定的 X 账号不存在")
        result = await XBrowserCollector(self.settings).post_status(
            account.handle, post.platform_post_id
        )
        remote_status = str(result.get("status") or "unknown")
        reason = str(result.get("reason") or "").strip()[:1000] or None
        now = datetime.now(UTC)
        post.last_availability_checked_at = now

        if remote_status == "available":
            post.availability_status = "available"
            post.availability_reason = None
            post.unavailable_since = None
            post.availability_failure_count = 0
        elif remote_status == "deleted":
            wait = timedelta(hours=max(1, self.settings.social_post_delete_confirm_hours))
            can_confirm = (
                post.availability_status == "unavailable"
                and post.availability_reason is not None
                and post.availability_reason.startswith("deleted_candidate:")
                and post.unavailable_since is not None
                and now - post.unavailable_since >= wait
            )
            post.availability_status = "deleted" if can_confirm else "unavailable"
            post.availability_reason = (
                f"deleted:{reason or 'X 明确返回已删除'}"
                if can_confirm
                else f"deleted_candidate:{reason or '等待二次确认'}"
            )
            post.unavailable_since = post.unavailable_since or now
            post.availability_failure_count += 1
        else:
            post.availability_status = remote_status if remote_status in {
                "protected", "account_unavailable"
            } else "unavailable"
            post.availability_reason = reason or "X 暂时没有返回可访问的帖子"
            post.unavailable_since = post.unavailable_since or now
            post.availability_failure_count += 1
        self.session.flush()
        return post

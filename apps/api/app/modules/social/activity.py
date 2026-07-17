import re
from dataclasses import dataclass
from hashlib import sha256

from sqlalchemy.orm import Session

from app.core.time import as_utc
from app.db.models import ActivityItem, SocialAccount, SocialPost
from app.modules.social.repository import SocialRepository

RELEASE_WORDS = ("新刊", "新作", "新しい本", "入稿", "脱稿", "予約開始", "販売開始")
EVENT_WORDS = ("コミケ", "コミティア", "イベント", "サークル参加", "スペース", "ブース")
SALES_WORDS = ("通販", "予約", "販売", "頒布", "委託", "再販", "在庫", "完売")
PROGRESS_WORDS = ("原稿", "作業中", "進捗", "線画", "彩色", "制作中", "描いて")
ART_WORDS = ("イラスト", "落書き", "らくがき", "色紙", "描きました", "絵です")
COLLAB_WORDS = ("コラボ", "合作", "ゲスト", "寄稿", "担当しました", "お仕事")
NOTICE_WORDS = ("延期", "中止", "欠席", "変更", "訂正", "お知らせ", "休止")
PERSONAL_WORDS = ("おはよう", "おやすみ", "体調", "旅行", "ごはん", "誕生日", "日記")
EVENT_RE = re.compile(r"(?<![A-Za-z0-9])C\d{2,3}(?![A-Za-z0-9])", re.I)

IMPORTANCE_RANK = {"low": 0, "normal": 1, "high": 2, "critical": 3}


@dataclass(frozen=True)
class ActivityAssessment:
    category: str
    headline: str
    summary: str
    importance: str
    confidence: float


def _contains(text: str, words: tuple[str, ...]) -> bool:
    folded = text.casefold()
    return any(word.casefold() in folded for word in words)


def assess_activity(post: SocialPost) -> ActivityAssessment | None:
    if post.post_type == "retweet":
        return None
    text = "\n".join(part for part in (post.text, post.ocr_text or "") if part).strip()
    compact = " ".join(text.split())
    if _contains(text, NOTICE_WORDS):
        category, importance, confidence = "schedule_notice", "critical", 0.92
    elif _contains(text, RELEASE_WORDS):
        category, importance, confidence = "release", "high", 0.90
    elif EVENT_RE.search(text) or _contains(text, EVENT_WORDS):
        category, importance, confidence = "event", "high", 0.86
    elif _contains(text, SALES_WORDS):
        category, importance, confidence = "sales", "high", 0.84
    elif _contains(text, PROGRESS_WORDS):
        category, importance, confidence = "creation_progress", "normal", 0.82
    elif _contains(text, COLLAB_WORDS):
        category, importance, confidence = "collaboration", "normal", 0.80
    elif _contains(text, ART_WORDS) or post.media:
        category, importance, confidence = "artwork", "normal", 0.74
    elif _contains(text, PERSONAL_WORDS):
        category, importance, confidence = "personal", "low", 0.68
    else:
        category, importance, confidence = "other", "low", 0.55
    if post.post_type == "reply":
        importance = "low" if importance == "normal" else importance
        confidence = max(0.45, confidence - 0.10)
    headline = compact[:120] or ("媒体动态" if post.media else "无文字动态")
    return ActivityAssessment(
        category=category,
        headline=headline,
        summary=compact[:1000],
        importance=importance,
        confidence=confidence,
    )


def activity_cluster_key(author_id: int, post: SocialPost, category: str) -> str:
    conversation = post.conversation_id or post.platform_post_id
    raw = f"{author_id}|{category}|{conversation}"
    return sha256(raw.encode("utf-8")).hexdigest()


class ActivityService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.repository = SocialRepository(session)

    def ingest(self, account: SocialAccount, post: SocialPost) -> ActivityItem | None:
        assessment = assess_activity(post)
        if not assessment:
            return None
        key = activity_cluster_key(account.author_id, post, assessment.category)
        activity = self.repository.activity_by_cluster(account.author_id, key)
        if activity is None:
            activity = ActivityItem(
                author_id=account.author_id,
                primary_post_id=post.id,
                cluster_key=key,
                category=assessment.category,
                headline=assessment.headline,
                summary=assessment.summary,
                importance=assessment.importance,
                confidence=assessment.confidence,
                started_at=post.posted_at,
                ended_at=post.posted_at,
            )
            self.session.add(activity)
            self.session.flush()
        else:
            is_latest = as_utc(post.posted_at) >= as_utc(activity.ended_at)
            if is_latest:
                activity.ended_at = post.posted_at
                activity.summary = assessment.summary or activity.summary
            if IMPORTANCE_RANK[assessment.importance] > IMPORTANCE_RANK[activity.importance]:
                activity.importance = assessment.importance
            activity.confidence = max(activity.confidence, assessment.confidence)
        self.repository.attach_activity_post(activity, post)
        return activity

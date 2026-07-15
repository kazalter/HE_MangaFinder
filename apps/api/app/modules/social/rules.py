import re
from dataclasses import dataclass, field
from hashlib import sha256
from urllib.parse import urlparse

from app.db.models import SocialPost

NEW_WORDS = ("新刊", "新作", "新しい本", "入稿", "脱稿", "描き下ろし")
PREVIEW_WORDS = ("サンプル", "sample", "試し読み", "本文見本")
COVER_WORDS = ("表紙", "カバー", "cover")
SALE_WORDS = ("通販", "予約", "頒布", "販売開始", "委託", "予約開始")
OLD_WORDS = ("既刊", "再販", "再版", "再録", "重版", "在庫")
CANCEL_WORDS = ("中止", "欠席", "延期", "落としました", "新刊ありません")
STORE_HOSTS = (
    "booth.pm",
    "melonbooks.co.jp",
    "toranoana.jp",
    "dlsite.com",
    "dmm.co.jp",
    "fanza.co.jp",
    "pixiv.net",
    "fanbox.cc",
)
EVENT_RE = re.compile(r"(?<![A-Za-z0-9])C(\d{2,3})(?![A-Za-z0-9])", re.I)
BOOTH_RE = re.compile(
    r"(?:東|西|南|日曜日|土曜日|月曜日|火曜日|水曜日|木曜日|金曜日)?\s*"
    r"[ぁ-んァ-ヶ一-龥A-Za-z]{0,4}\s*[-－ー]\s*\d{1,2}[abAB]?"
)
TITLE_RE = re.compile(r"[『「【]([^』」】]{2,160})[』」】]")


@dataclass(frozen=True)
class RuleAssessment:
    candidate: bool
    kind: str = "other"
    confidence: float = 0.0
    title: str | None = None
    event_code: str | None = None
    booth: str | None = None
    store_urls: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    counter_evidence: list[str] = field(default_factory=list)
    independent_signals: int = 0
    explicit_new_work: bool = False


def _matches(text: str, words: tuple[str, ...]) -> list[str]:
    folded = text.casefold()
    return [word for word in words if word.casefold() in folded]


def assess_post(post: SocialPost) -> RuleAssessment:
    if post.post_type == "retweet":
        return RuleAssessment(candidate=False, counter_evidence=["普通转推"])

    combined = "\n".join(part for part in (post.text, post.ocr_text or "") if part)
    new_hits = _matches(combined, NEW_WORDS)
    preview_hits = _matches(combined, PREVIEW_WORDS)
    cover_hits = _matches(combined, COVER_WORDS)
    sale_hits = _matches(combined, SALE_WORDS)
    old_hits = _matches(combined, OLD_WORDS)
    cancel_hits = _matches(combined, CANCEL_WORDS)
    event_match = EVENT_RE.search(combined)
    booth_match = BOOTH_RE.search(combined)
    title_match = TITLE_RE.search(combined)
    stores = [
        url
        for url in post.links
        if any(
            urlparse(url).hostname == host
            or (urlparse(url).hostname or "").endswith(f".{host}")
            for host in STORE_HOSTS
        )
    ]

    evidence: list[str] = []
    counter: list[str] = []
    if new_hits:
        evidence.append(f"明确新作词：{', '.join(new_hits)}")
    if preview_hits:
        evidence.append(f"样张词：{', '.join(preview_hits)}")
    if cover_hits:
        evidence.append(f"封面词：{', '.join(cover_hits)}")
    if sale_hits or stores:
        evidence.append("出现销售/预订信息")
    if event_match:
        evidence.append(f"展会编号：C{event_match.group(1)}")
    if title_match:
        evidence.append(f"疑似标题：{title_match.group(1).strip()}")
    if post.media:
        evidence.append("帖子包含媒体")
    if old_hits:
        counter.append(f"旧作/再版词：{', '.join(old_hits)}")
    if cancel_hits:
        counter.append(f"取消/延期词：{', '.join(cancel_hits)}")

    if cancel_hits:
        kind = "cancellation" if any(word in combined for word in ("中止", "欠席")) else "delay"
    elif old_hits and not new_hits:
        kind = "reprint"
    elif new_hits:
        kind = "new_release"
    elif preview_hits:
        kind = "release_preview"
    elif cover_hits:
        kind = "cover_reveal"
    elif stores or sale_hits:
        kind = "preorder"
    elif event_match:
        kind = "event_participation"
    else:
        kind = "other"

    candidate = bool(
        new_hits
        or preview_hits
        or cover_hits
        or sale_hits
        or stores
        or event_match
        or cancel_hits
        or old_hits
    )
    confidence = 0.0
    if new_hits:
        confidence += 0.68
    elif preview_hits or cover_hits or stores:
        confidence += 0.48
    elif event_match or old_hits or cancel_hits:
        confidence += 0.38
    independent = 0
    for present, weight in (
        (bool(title_match), 0.08),
        (bool(post.media), 0.06),
        (bool(stores or sale_hits), 0.10),
        (bool(event_match), 0.05),
        (bool(booth_match), 0.04),
    ):
        if present:
            independent += 1
            confidence += weight
    if old_hits and not new_hits:
        confidence = min(confidence, 0.68)
    confidence = min(confidence, 0.95)
    return RuleAssessment(
        candidate=candidate,
        kind=kind,
        confidence=confidence,
        title=title_match.group(1).strip() if title_match else None,
        event_code=f"C{event_match.group(1)}".upper() if event_match else None,
        booth=booth_match.group(0).strip() if booth_match else None,
        store_urls=stores,
        evidence=evidence,
        counter_evidence=counter,
        independent_signals=independent,
        explicit_new_work=bool(new_hits),
    )


def cluster_key(author_id: int, assessment: RuleAssessment, post: SocialPost) -> str:
    identity = "|".join(
        [
            str(author_id),
            (assessment.title or "").casefold().strip(),
            assessment.event_code or "",
            next(iter(assessment.store_urls), ""),
            post.conversation_id or post.platform_post_id,
        ]
    )
    return sha256(identity.encode("utf-8")).hexdigest()

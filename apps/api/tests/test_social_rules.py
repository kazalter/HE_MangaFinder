from datetime import UTC, datetime

from app.db.models import SocialPost
from app.modules.social.rules import assess_post


def post(text: str, *, post_type: str = "original", media: list[dict] | None = None) -> SocialPost:
    return SocialPost(
        account_id=1,
        platform_post_id="123",
        post_type=post_type,
        text=text,
        url="https://x.com/creator/status/123",
        media=media or [],
        links=[],
        raw_metadata={},
        content_hash="hash",
        posted_at=datetime.now(UTC),
    )


def test_retweet_is_never_a_candidate() -> None:
    result = assess_post(post("C108新刊『強い新作』予約開始", post_type="retweet"))
    assert result.candidate is False


def test_event_code_alone_is_participation_not_new_work() -> None:
    result = assess_post(post("C108に参加します。東A-12aです"))
    assert result.candidate is True
    assert result.kind == "event_participation"
    assert result.explicit_new_work is False


def test_reprint_is_not_a_new_release() -> None:
    result = assess_post(post("既刊『前の本』を再販します"))
    assert result.kind == "reprint"
    assert result.explicit_new_work is False
    assert result.confidence < 0.92


def test_explicit_new_release_collects_independent_evidence() -> None:
    item = post(
        "C108新刊『星の本』の表紙です。BOOTHで予約開始",
        media=[{"type": "image", "url": "https://pbs.twimg.com/media/a.jpg"}],
    )
    item.links = ["https://creator.booth.pm/items/123"]
    result = assess_post(item)
    assert result.kind == "new_release"
    assert result.title == "星の本"
    assert result.event_code == "C108"
    assert result.independent_signals >= 2
    assert result.confidence >= 0.92

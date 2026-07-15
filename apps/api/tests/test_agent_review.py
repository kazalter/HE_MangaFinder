from datetime import UTC, datetime

import httpx
import pytest
from PIL import Image, ImageDraw
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.base import Base
from app.db.models import AgentReview, Author, MergeSuggestion, WorkGroup
from app.modules.agent_review.candidates import build_candidate_evidence
from app.modules.agent_review.client import OpenAICompatibleReviewer, ReviewResponse
from app.modules.agent_review.grounding import validate_grounding
from app.modules.agent_review.schemas import AgentVerdict
from app.modules.agent_review.service import AgentReviewService
from app.modules.catalog.aggregation import AggregationService
from app.modules.catalog.cover_fingerprint import fingerprint_image
from app.modules.catalog.repairs import repair_wnacg_upload_years
from app.modules.catalog.repository import CatalogRepository
from app.providers.base import DiscoveredWork


class FakeReviewer:
    def __init__(self, verdict: AgentVerdict) -> None:
        self.verdict = verdict
        self.calls = 0

    async def review(self, evidence: object) -> ReviewResponse:
        self.calls += 1
        return ReviewResponse(
            verdict=self.verdict,
            raw_output=self.verdict.model_dump(mode="json"),
        )


def make_candidate(session: Session, left_title: str, right_title: str) -> MergeSuggestion:
    author = Author(name="mignon")
    session.add(author)
    session.flush()
    catalog = CatalogRepository(session)
    aggregation = AggregationService(session)
    first = catalog.upsert(
        author.id,
        "wnacg",
        DiscoveredWork(
            external_id="left",
            title=left_title,
            source_url="https://example.test/left",
            raw_metadata={"page_count": 30},
        ),
    )
    left = aggregation.assign_without_cover(first, author)
    second = catalog.upsert(
        author.id,
        "hanimeone",
        DiscoveredWork(
            external_id="right",
            title=right_title,
            source_url="https://example.test/right",
            raw_metadata={"page_count": 31},
        ),
    )
    right = aggregation.assign_without_cover(second, author)
    if right.id == left.id:
        right = aggregation.split_member(left, second.id)
    suggestion = MergeSuggestion(
        source_group_id=left.id,
        target_group_id=right.id,
        confidence=0.78,
        reasons=["测试候选"],
    )
    session.add(suggestion)
    session.flush()
    return suggestion


async def test_agent_review_is_grounded_persisted_and_read_only() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(agent_enabled=True, agent_model="test-model", _env_file=None)
    verdict = AgentVerdict(
        decision="same_work",
        confidence=0.95,
        canonical_title="Ocean Belly",
        relation="translation",
        evidence=["core_title_match", "number_match", "page_count_match"],
        conflicts=[],
        rationale="作者、编号和页数一致，标题可能是翻译差异。",
        recommended_action="suggest_merge",
    )
    reviewer = FakeReviewer(verdict)

    with Session(engine) as session:
        suggestion = make_candidate(session, "Ocean Belly 2", "Ocean Belly 2 [汉化]")
        original_group_count = len(list(session.scalars(select(WorkGroup))))
        review, outcome = await AgentReviewService(
            session, settings, reviewer
        ).review_suggestion(suggestion.id)
        session.commit()

        assert outcome == "reviewed"
        assert review is not None and review.decision == "same_work"
        assert review.rationale.startswith("Agent 倾向同一作品")
        assert review.raw_output["rationale"] == verdict.rationale
        assert reviewer.calls == 1
        assert suggestion.status == "pending"
        assert len(list(session.scalars(select(WorkGroup)))) == original_group_count
        assert session.scalar(select(AgentReview)).input_snapshot["left"]["editions"]


def test_common_series_suffix_does_not_become_identity_evidence() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        suggestion = make_candidate(
            session,
            "[ラマンダ] アコちゃんサンタのプレゼント (ブルーアーカイブ) [中国翻訳]",
            "[ラマンダ] サンタアスナとカリンのプレゼント (ブルーアーカイブ)",
        )
        evidence = build_candidate_evidence(
            suggestion,
            session.get(WorkGroup, suggestion.source_group_id),
            session.get(WorkGroup, suggestion.target_group_id),
        )

        assert evidence.core_title_similarity < 0.68
        assert evidence.shared_context == ["ブルーアーカイブ"]
        assert "number_match" not in evidence.available_evidence
        assert "core_title_similarity" not in evidence.available_evidence

        left = session.get(WorkGroup, suggestion.source_group_id)
        right = session.get(WorkGroup, suggestion.target_group_id)
        first_cover = Image.new("RGB", (320, 440), "white")
        second_cover = Image.new("RGB", (320, 440), "#17294f")
        first_draw = ImageDraw.Draw(first_cover)
        second_draw = ImageDraw.Draw(second_cover)
        first_draw.rectangle((25, 25, 295, 415), fill="#bd4232")
        second_draw.ellipse((35, 70, 285, 320), fill="#dfc64a")
        left.members[0].work.fingerprint.cover_fingerprint = fingerprint_image(first_cover)
        right.members[0].work.fingerprint.cover_fingerprint = fingerprint_image(second_cover)
        left.members[0].work.fingerprint.page_count = 9
        right.members[0].work.fingerprint.page_count = 4
        evidence = build_candidate_evidence(suggestion, left, right)

        assert "cover_dissimilar" in evidence.soft_conflicts
        assert "page_count_mismatch" in evidence.soft_conflicts
        assert evidence.page_count_ratio == pytest.approx(4 / 9, rel=1e-4)

        author = session.scalar(select(Author))
        assert AggregationService(session).prune_pending_suggestions(author.id) == 1
        assert session.get(MergeSuggestion, suggestion.id) is None


def test_high_confidence_same_work_without_independent_support_is_downgraded() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        suggestion = make_candidate(session, "Nure Onaka", "Nugi Onaka")
        left = session.get(WorkGroup, suggestion.source_group_id)
        right = session.get(WorkGroup, suggestion.target_group_id)
        left.members[0].work.fingerprint.page_count = None
        right.members[0].work.fingerprint.page_count = None
        evidence = build_candidate_evidence(suggestion, left, right)
        verdict = AgentVerdict(
            decision="same_work",
            confidence=0.95,
            canonical_title="Nure Onaka",
            relation="translation",
            evidence=["core_title_similarity"],
            conflicts=[],
            rationale="标题看起来相似。",
            recommended_action="suggest_merge",
        )

        calibrated = validate_grounding(evidence, verdict)

        assert calibrated.decision == "uncertain"
        assert calibrated.confidence == 0.6
        assert calibrated.recommended_action == "human_review"
        assert "insufficient_evidence" in calibrated.conflicts


def test_model_context_codes_are_removed_before_calibration() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        suggestion = make_candidate(session, "Ocean Belly 2", "Ocean Belly 2 [汉化]")
        evidence = build_candidate_evidence(
            suggestion,
            session.get(WorkGroup, suggestion.source_group_id),
            session.get(WorkGroup, suggestion.target_group_id),
        )
        verdict = AgentVerdict(
            decision="same_work",
            confidence=0.95,
            canonical_title="Ocean Belly 2",
            relation="translation",
            evidence=[
                "normalized_title_match",
                "number_match",
                "page_count_match",
                "author_match",
            ],
            conflicts=[],
            rationale="标题、编号和页数相符。",
            recommended_action="suggest_merge",
        )

        calibrated = validate_grounding(evidence, verdict)

        assert calibrated.decision == "same_work"
        assert calibrated.evidence == [
            "core_title_match",
            "number_match",
            "page_count_match",
        ]


def test_model_unsupported_conflict_is_removed_instead_of_failing_review() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        suggestion = make_candidate(session, "Ocean Belly", "Ocean Tummy")
        evidence = build_candidate_evidence(
            suggestion,
            session.get(WorkGroup, suggestion.source_group_id),
            session.get(WorkGroup, suggestion.target_group_id),
        )
        verdict = AgentVerdict(
            decision="different_work",
            confidence=0.96,
            canonical_title="",
            relation="unrelated",
            evidence=[],
            conflicts=["core_title_mismatch"],
            rationale="模型给出了规则证据中不存在的冲突。",
            recommended_action="keep_separate",
        )

        calibrated = validate_grounding(evidence, verdict)

        assert calibrated.conflicts == []
        assert calibrated.confidence == 0.85


async def test_hard_number_conflict_never_reaches_model() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(agent_enabled=True, agent_model="test-model", _env_file=None)
    reviewer = FakeReviewer(
        AgentVerdict(
            decision="uncertain",
            confidence=0.5,
            canonical_title="",
            relation="unknown",
            evidence=[],
            conflicts=["insufficient_evidence"],
            rationale="证据不足。",
            recommended_action="human_review",
        )
    )

    with Session(engine) as session:
        suggestion = make_candidate(session, "JK x ONAKA #01", "JK x ONAKA #02")
        left = session.get(WorkGroup, suggestion.source_group_id)
        right = session.get(WorkGroup, suggestion.target_group_id)
        evidence = build_candidate_evidence(suggestion, left, right)
        assert "number_mismatch" in evidence.hard_conflicts

        review, outcome = await AgentReviewService(
            session, settings, reviewer
        ).review_suggestion(suggestion.id)

        assert outcome == "blocked"
        assert review is not None and review.status == "blocked"
        assert reviewer.calls == 0


def test_year_and_page_conflicts_are_soft_warnings() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        suggestion = make_candidate(session, "Ocean Belly", "Ocean Tummy")
        left = session.get(WorkGroup, suggestion.source_group_id)
        right = session.get(WorkGroup, suggestion.target_group_id)
        left.members[0].work.year = 2019
        right.members[0].work.year = 2022
        left.members[0].work.fingerprint.page_count = 20
        right.members[0].work.fingerprint.page_count = 35

        evidence = build_candidate_evidence(suggestion, left, right)

        assert evidence.hard_conflicts == []
        assert set(evidence.soft_conflicts) == {
            "year_mismatch",
            "page_count_mismatch",
            "insufficient_evidence",
        }


def test_repairs_legacy_wnacg_upload_year_without_losing_sort_date() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        author = Author(name="Author")
        session.add(author)
        session.flush()
        work = CatalogRepository(session).upsert(
            author.id,
            "wnacg",
            DiscoveredWork(
                external_id="legacy-year",
                title="Legacy title",
                source_url="https://example.test/legacy",
                year=2021,
                source_updated_at=datetime(2021, 7, 16, tzinfo=UTC),
            ),
        )
        group = AggregationService(session).assign_without_cover(work, author)
        assert group.year == 2021

        assert repair_wnacg_upload_years(session) == 1
        assert work.year is None
        assert work.sources[0].source_updated_at.year == 2021
        assert work.sources[0].raw_metadata["upload_year_repaired"] is True
        assert group.year is None


async def test_openai_compatible_client_requires_schema_json() -> None:
    verdict = {
        "decision": "uncertain",
        "confidence": 0.55,
        "canonical_title": "",
        "relation": "unknown",
        "evidence": ["title_similarity"],
        "conflicts": ["insufficient_evidence"],
        "rationale": "标题相似，但缺少足够的独立证据。",
        "recommended_action": "human_review",
    }
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={"choices": [{"message": {"content": __import__("json").dumps(verdict)}}]},
        )
    )
    client = httpx.AsyncClient(transport=transport)
    settings = Settings(
        agent_enabled=True,
        agent_model="test-model",
        agent_base_url="https://model.example/v1",
        _env_file=None,
    )
    reviewer = OpenAICompatibleReviewer(settings, client)

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        suggestion = make_candidate(session, "Ocean Belly", "Ocean Tummy")
        evidence = build_candidate_evidence(
            suggestion,
            session.get(WorkGroup, suggestion.source_group_id),
            session.get(WorkGroup, suggestion.target_group_id),
        )
        response = await reviewer.review(evidence)
        assert response.verdict.decision == "uncertain"
    await client.aclose()


async def test_deepseek_client_uses_json_object_mode() -> None:
    requests: list[httpx.Request] = []
    verdict = {
        "decision": "uncertain",
        "confidence": 0.5,
        "canonical_title": "",
        "relation": "unknown",
        "evidence": [],
        "conflicts": ["insufficient_evidence"],
        "rationale": "证据不足。",
        "recommended_action": "human_review",
    }

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": __import__("json").dumps(verdict)}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    settings = Settings(
        agent_enabled=True,
        agent_provider="deepseek",
        agent_model="deepseek-v4-flash",
        agent_base_url="https://api.deepseek.com",
        _env_file=None,
    )
    reviewer = OpenAICompatibleReviewer(settings, client)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        suggestion = make_candidate(session, "Ocean Belly", "Ocean Tummy")
        evidence = build_candidate_evidence(
            suggestion,
            session.get(WorkGroup, suggestion.source_group_id),
            session.get(WorkGroup, suggestion.target_group_id),
        )
        await reviewer.review(evidence)

    body = __import__("json").loads(requests[0].content)
    assert body["response_format"] == {"type": "json_object"}
    assert body["thinking"] == {"type": "disabled"}
    assert requests[0].url == "https://api.deepseek.com/chat/completions"
    await client.aclose()

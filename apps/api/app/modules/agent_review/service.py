import logging

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import AgentReview
from app.modules.agent_review.candidates import build_candidate_evidence, evidence_hash
from app.modules.agent_review.client import AggregationReviewer, build_reviewer
from app.modules.agent_review.errors import AgentReviewError
from app.modules.agent_review.grounding import validate_grounding
from app.modules.agent_review.repository import AgentReviewRepository
from app.modules.catalog.group_repository import WorkGroupRepository

logger = logging.getLogger(__name__)


class AgentReviewService:
    def __init__(
        self,
        session: Session,
        settings: Settings,
        reviewer: AggregationReviewer | None = None,
    ) -> None:
        self.session = session
        self.settings = settings
        self.reviewer = reviewer
        self.reviews = AgentReviewRepository(session)
        self.groups = WorkGroupRepository(session)

    async def run_pending(self, maximum: int | None = None) -> dict[str, int]:
        reviewer = self.reviewer or build_reviewer(self.settings)
        limit = min(maximum or self.settings.agent_max_reviews_per_run, 100)
        suggestions = self.groups.suggestions("pending")[:limit]
        counts = {"reviewed": 0, "cached": 0, "blocked": 0, "failed": 0}
        for suggestion in suggestions:
            try:
                result, outcome = await self.review_suggestion(suggestion.id, reviewer)
                counts[outcome] += 1
                if result:
                    logger.info(
                        "Agent review %s: %s %.3f",
                        result.id,
                        result.decision,
                        result.confidence or 0,
                    )
            except AgentReviewError as exc:
                logger.warning("Agent review failed for suggestion %s: %s", suggestion.id, exc)
                counts["failed"] += 1
            self.session.commit()
        return counts

    async def review_suggestion(
        self, suggestion_id: int, reviewer: AggregationReviewer | None = None
    ) -> tuple[AgentReview | None, str]:
        suggestion = self.groups.suggestion(suggestion_id)
        if suggestion is None or suggestion.status != "pending":
            return None, "blocked"
        left = self.groups.get(suggestion.source_group_id)
        right = self.groups.get(suggestion.target_group_id)
        if left is None or right is None:
            return None, "blocked"
        evidence = build_candidate_evidence(suggestion, left, right)
        digest = evidence_hash(evidence)
        if self.reviews.is_constrained(evidence.candidate_key):
            return None, "blocked"
        if evidence.hard_conflicts:
            review = self.reviews.record(
                evidence=evidence,
                evidence_hash=digest,
                provider="deterministic_guard",
                model="hard_constraints",
                prompt_version=self.settings.agent_prompt_version,
                status="blocked",
                error="硬冲突阻止 Agent 将候选判为同一作品",
            )
            return review, "blocked"
        cached = self.reviews.cached(
            candidate_key=evidence.candidate_key,
            evidence_hash=digest,
            model=self.settings.agent_model,
            prompt_version=self.settings.agent_prompt_version,
        )
        if cached and cached.suggestion_id == suggestion_id:
            return cached, "cached"
        reviewer = reviewer or self.reviewer or build_reviewer(self.settings)
        raw_output: dict[str, object] | None = None
        try:
            response = await reviewer.review(evidence)
            raw_output = response.raw_output
            verdict = validate_grounding(evidence, response.verdict)
            review = self.reviews.record(
                evidence=evidence,
                evidence_hash=digest,
                provider=self.settings.agent_provider,
                model=self.settings.agent_model,
                prompt_version=self.settings.agent_prompt_version,
                status="succeeded",
                verdict=verdict,
                raw_output=response.raw_output,
            )
            # Phase 1 is deliberately read-only. Human acceptance remains mandatory.
            return review, "reviewed"
        except AgentReviewError as exc:
            self.reviews.record(
                evidence=evidence,
                evidence_hash=digest,
                provider=self.settings.agent_provider,
                model=self.settings.agent_model,
                prompt_version=self.settings.agent_prompt_version,
                status="failed",
                raw_output=raw_output,
                error=str(exc),
            )
            raise

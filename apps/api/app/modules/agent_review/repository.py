from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session, defer

from app.db.models import AgentReview, PairConstraint
from app.modules.agent_review.schemas import (
    SCHEMA_VERSION,
    AgentVerdict,
    CandidateEvidence,
)


class AgentReviewRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list_recent(self, limit: int = 50) -> list[AgentReview]:
        return list(
            self.session.scalars(
                select(AgentReview)
                .options(defer(AgentReview.input_snapshot), defer(AgentReview.raw_output))
                .order_by(AgentReview.created_at.desc())
                .limit(limit)
            )
        )

    def latest_for_suggestion(self, suggestion_id: int) -> AgentReview | None:
        return self.session.scalar(
            select(AgentReview)
            .options(defer(AgentReview.input_snapshot), defer(AgentReview.raw_output))
            .where(AgentReview.suggestion_id == suggestion_id)
            .order_by(AgentReview.created_at.desc(), AgentReview.id.desc())
            .limit(1)
        )

    def latest_by_suggestion_ids(
        self, suggestion_ids: Sequence[int]
    ) -> dict[int, AgentReview]:
        unique_ids = sorted({int(sid) for sid in suggestion_ids})
        if not unique_ids:
            return {}
        latest: dict[int, AgentReview] = {}
        for start in range(0, len(unique_ids), 500):
            batch = unique_ids[start : start + 500]
            rows = self.session.scalars(
                select(AgentReview)
                .options(defer(AgentReview.input_snapshot), defer(AgentReview.raw_output))
                .where(AgentReview.suggestion_id.in_(batch))
                .order_by(AgentReview.created_at.desc(), AgentReview.id.desc())
            )
            for row in rows:
                if row.suggestion_id is not None and row.suggestion_id not in latest:
                    latest[row.suggestion_id] = row
        return latest

    def latest_blocked(
        self,
        *,
        suggestion_id: int,
        candidate_key: str,
        evidence_hash: str,
    ) -> AgentReview | None:
        return self.session.scalar(
            select(AgentReview)
            .options(defer(AgentReview.input_snapshot), defer(AgentReview.raw_output))
            .where(
                AgentReview.suggestion_id == suggestion_id,
                AgentReview.candidate_key == candidate_key,
                AgentReview.evidence_hash == evidence_hash,
                AgentReview.schema_version == SCHEMA_VERSION,
                AgentReview.status == "blocked",
            )
            .order_by(AgentReview.created_at.desc(), AgentReview.id.desc())
            .limit(1)
        )

    def cached(
        self,
        *,
        candidate_key: str,
        evidence_hash: str,
        model: str,
        prompt_version: str,
    ) -> AgentReview | None:
        return self.session.scalar(
            select(AgentReview)
            .where(
                AgentReview.candidate_key == candidate_key,
                AgentReview.evidence_hash == evidence_hash,
                AgentReview.model == model,
                AgentReview.prompt_version == prompt_version,
                AgentReview.schema_version == SCHEMA_VERSION,
                AgentReview.status == "succeeded",
            )
            .order_by(AgentReview.created_at.desc(), AgentReview.id.desc())
            .limit(1)
        )

    def record(
        self,
        *,
        evidence: CandidateEvidence,
        evidence_hash: str,
        provider: str,
        model: str,
        prompt_version: str,
        status: str,
        verdict: AgentVerdict | None = None,
        raw_output: dict[str, object] | None = None,
        error: str | None = None,
    ) -> AgentReview:
        review = AgentReview(
            suggestion_id=evidence.suggestion_id,
            candidate_key=evidence.candidate_key,
            evidence_hash=evidence_hash,
            provider=provider,
            model=model,
            prompt_version=prompt_version,
            schema_version=SCHEMA_VERSION,
            status=status,
            decision=verdict.decision if verdict else None,
            confidence=verdict.confidence if verdict else None,
            relation=verdict.relation if verdict else None,
            canonical_title=verdict.canonical_title if verdict else None,
            evidence_codes=list(verdict.evidence) if verdict else [],
            conflict_codes=(
                list(verdict.conflicts) if verdict else list(evidence.hard_conflicts)
            ),
            rationale=verdict.rationale if verdict else None,
            input_snapshot=evidence.model_dump(mode="json"),
            raw_output=raw_output or {},
            error=error[:4000] if error else None,
        )
        self.session.add(review)
        self.session.flush()
        return review

    def constrain_pair(
        self,
        candidate_key: str,
        *,
        origin: str,
        review_id: int | None = None,
        note: str | None = None,
    ) -> PairConstraint:
        constraint = self.session.scalar(
            select(PairConstraint).where(PairConstraint.candidate_key == candidate_key)
        )
        if constraint is None:
            constraint = PairConstraint(candidate_key=candidate_key)
            self.session.add(constraint)
        constraint.decision = "different_work"
        constraint.origin = origin
        constraint.review_id = review_id
        constraint.note = note
        self.session.flush()
        return constraint

    def is_constrained(self, candidate_key: str) -> bool:
        return self.session.scalar(
            select(PairConstraint.id).where(
                PairConstraint.candidate_key == candidate_key,
                PairConstraint.decision == "different_work",
            )
        ) is not None

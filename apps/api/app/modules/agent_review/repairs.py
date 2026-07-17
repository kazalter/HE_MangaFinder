from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AgentReview
from app.modules.agent_review.grounding import render_grounded_rationale
from app.modules.agent_review.schemas import AgentVerdict, CandidateEvidence


def repair_historical_rationales(session: Session) -> int:
    """Replace legacy model prose with deterministic Chinese evidence summaries."""
    reviews = list(
        session.scalars(
            select(AgentReview).where(
                AgentReview.status == "succeeded",
                AgentReview.rationale.contains("模型具体理由："),
            )
        )
    )
    repaired = 0
    actions = {
        "same_work": "suggest_merge",
        "different_work": "keep_separate",
        "uncertain": "human_review",
    }
    for review in reviews:
        if not review.decision or review.confidence is None or not review.relation:
            continue
        try:
            evidence = CandidateEvidence.model_validate(review.input_snapshot)
            verdict = AgentVerdict(
                decision=review.decision,
                confidence=review.confidence,
                canonical_title=review.canonical_title or "",
                relation=review.relation,
                evidence=review.evidence_codes or [],
                conflicts=review.conflict_codes or [],
                rationale="历史审核说明已依据结构化证据重新生成。",
                recommended_action=actions[review.decision],
            )
        except (KeyError, ValidationError):
            continue
        review.rationale = render_grounded_rationale(evidence, verdict)
        repaired += 1
    if repaired:
        session.flush()
    return repaired

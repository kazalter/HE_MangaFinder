from app.modules.agent_review.errors import AgentOutputError
from app.modules.agent_review.schemas import AgentVerdict, CandidateEvidence


def validate_grounding(
    evidence: CandidateEvidence, verdict: AgentVerdict
) -> AgentVerdict:
    unsupported_evidence = set(verdict.evidence) - set(evidence.available_evidence)
    allowed_conflicts = {
        *evidence.hard_conflicts,
        *evidence.soft_conflicts,
        "insufficient_evidence",
    }
    unsupported_conflicts = set(verdict.conflicts) - allowed_conflicts
    if unsupported_evidence:
        raise AgentOutputError(
            f"模型引用了不存在的证据：{', '.join(sorted(unsupported_evidence))}"
        )
    if unsupported_conflicts:
        raise AgentOutputError(
            f"模型引用了不存在的冲突：{', '.join(sorted(unsupported_conflicts))}"
        )
    if evidence.hard_conflicts and verdict.decision == "same_work":
        raise AgentOutputError("模型试图越过硬冲突判定为同一作品")
    expected_actions = {
        "same_work": "suggest_merge",
        "different_work": "keep_separate",
        "uncertain": "human_review",
    }
    if verdict.recommended_action != expected_actions[verdict.decision]:
        raise AgentOutputError("模型决定与建议动作不一致")
    if verdict.decision == "different_work" and verdict.relation not in {
        "unrelated",
        "unknown",
    }:
        raise AgentOutputError("不同作品不能声明为版本关系")
    if verdict.decision == "same_work" and verdict.relation in {"unrelated", "unknown"}:
        raise AgentOutputError("同一作品必须说明版本关系")
    return verdict.model_copy(update={"rationale": _grounded_rationale(verdict)})


def _grounded_rationale(verdict: AgentVerdict) -> str:
    decisions = {
        "same_work": "倾向同一作品",
        "different_work": "倾向不同作品",
        "uncertain": "证据不足，需人工判断",
    }
    relations = {
        "same_edition": "相同版本",
        "translation": "翻译版本",
        "colored": "上色版本",
        "uncensored": "无码版本",
        "remaster": "重制版本",
        "mixed_variants": "多个版本差异",
        "unrelated": "无版本关系",
        "unknown": "关系未知",
    }
    labels = {
        "title_similarity": "标题相似",
        "normalized_title_match": "标准化标题一致",
        "number_match": "作品编号一致",
        "cover_hash_match": "封面感知哈希相似",
        "page_count_match": "页数相近",
        "year_match": "年份相符",
        "author_match": "作者一致",
        "provider_overlap": "来源有重合",
        "variant_difference": "存在版本标签差异",
        "language_difference": "存在语言差异",
        "number_mismatch": "作品编号冲突",
        "author_mismatch": "作者冲突",
        "page_count_mismatch": "页数明显冲突",
        "year_mismatch": "年份冲突",
        "insufficient_evidence": "独立证据不足",
    }
    parts = [
        f"Agent {decisions[verdict.decision]}（{verdict.confidence:.0%}）",
        f"版本关系：{relations[verdict.relation]}",
    ]
    if verdict.evidence:
        parts.append("已验证证据：" + "、".join(labels[code] for code in verdict.evidence))
    if verdict.conflicts:
        parts.append("冲突：" + "、".join(labels[code] for code in verdict.conflicts))
    return "；".join(parts) + "。"

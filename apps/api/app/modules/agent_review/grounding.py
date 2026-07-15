from app.modules.agent_review.errors import AgentOutputError
from app.modules.agent_review.schemas import AgentVerdict, CandidateEvidence

_STRONG_IDENTITY = {
    "core_title_match",
    "source_alias_match",
    "number_match",
    "cover_hash_strong",
    "cover_crop_match",
}
_IDENTITY_DIMENSIONS = {
    "core_title_match": "title",
    "source_alias_match": "title",
    "core_title_similarity": "title",
    "normalized_title_match": "title",
    "title_similarity": "title",
    "number_match": "number",
    "cover_hash_strong": "cover",
    "cover_hash_weak": "cover",
    "cover_hash_match": "cover",
    "cover_crop_match": "cover",
    "page_count_match": "pages",
    "year_match": "year",
}
_SEVERE_DIFFERENCES = {
    "core_title_mismatch",
    "cover_dissimilar",
    "page_count_mismatch",
}


def validate_grounding(
    evidence: CandidateEvidence, verdict: AgentVerdict
) -> AgentVerdict:
    verdict = _normalize_model_codes(evidence, verdict)
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

    original_rationale = verdict.rationale
    calibrated, notes = _calibrate(evidence, verdict)
    return calibrated.model_copy(
        update={
            "rationale": _grounded_rationale(
                calibrated, original_rationale, notes
            )
        }
    )


def _normalize_model_codes(
    evidence: CandidateEvidence, verdict: AgentVerdict
) -> AgentVerdict:
    available = set(evidence.available_evidence)
    context = set(evidence.context_only)
    normalized: list[str] = []
    aliases = {
        "core_title_match": ("source_alias_match",),
        "normalized_title_match": ("core_title_match", "source_alias_match"),
        "title_similarity": ("core_title_similarity",),
        "cover_hash_match": (
            "cover_crop_match",
            "cover_hash_strong",
            "cover_hash_weak",
        ),
    }
    for code in verdict.evidence:
        selected = code
        if selected not in available:
            selected = next(
                (
                    replacement
                    for replacement in aliases.get(code, ())
                    if replacement in available
                ),
                "",
            )
        if selected in context:
            selected = ""
        if selected and selected not in normalized:
            normalized.append(selected)
    allowed_conflicts = {
        *evidence.hard_conflicts,
        *evidence.soft_conflicts,
        "insufficient_evidence",
    }
    normalized_conflicts = [
        code for code in verdict.conflicts if code in allowed_conflicts
    ]
    return verdict.model_copy(
        update={"evidence": normalized, "conflicts": normalized_conflicts}
    )


def _calibrate(
    evidence: CandidateEvidence, verdict: AgentVerdict
) -> tuple[AgentVerdict, list[str]]:
    notes: list[str] = []
    selected_evidence = set(verdict.evidence)
    selected_conflicts = set(verdict.conflicts)

    if verdict.decision == "same_work":
        strong = bool(selected_evidence & _STRONG_IDENTITY)
        dimensions = {
            dimension
            for code, dimension in _IDENTITY_DIMENSIONS.items()
            if code in selected_evidence
        }
        severe = set(evidence.soft_conflicts) & _SEVERE_DIFFERENCES
        exact_title = bool(
            selected_evidence & {"core_title_match", "source_alias_match"}
        )
        must_downgrade = (
            not strong
            or len(dimensions) < 2
            or (len(severe) >= 2 and not exact_title)
        )
        if must_downgrade:
            notes.append("同作结论缺少一个强身份信号和另一个独立支持信号")
            conflicts = list(
                dict.fromkeys([*verdict.conflicts, "insufficient_evidence"])
            )
            return (
                verdict.model_copy(
                    update={
                        "decision": "uncertain",
                        "confidence": min(verdict.confidence, 0.6),
                        "relation": "unknown",
                        "conflicts": conflicts,
                        "recommended_action": "human_review",
                    }
                ),
                notes,
            )
        if verdict.confidence > 0.95:
            notes.append("同作置信度按服务端校准上限调整为 95%")
            verdict = verdict.model_copy(update={"confidence": 0.95})

    elif verdict.decision == "different_work":
        explicit_differences = selected_conflicts & set(evidence.soft_conflicts)
        if verdict.confidence > 0.85 and not explicit_differences:
            notes.append("未引用明确负向证据，不同作品置信度上限调整为 85%")
            verdict = verdict.model_copy(update={"confidence": 0.85})
    elif verdict.confidence > 0.7:
        notes.append("不确定结论的置信度上限调整为 70%")
        verdict = verdict.model_copy(update={"confidence": 0.7})

    return verdict, notes


def _grounded_rationale(
    verdict: AgentVerdict, model_rationale: str, calibration_notes: list[str]
) -> str:
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
        "core_title_match": "核心标题一致",
        "core_title_similarity": "核心标题相似",
        "source_alias_match": "来源别名一致",
        "number_match": "非空作品编号一致",
        "cover_hash_match": "封面感知哈希相似",
        "cover_hash_strong": "封面强相似",
        "cover_hash_weak": "封面弱相似",
        "cover_crop_match": "同图裁切匹配",
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
        "core_title_mismatch": "核心标题主体不同",
        "cover_dissimilar": "封面明显不同",
        "insufficient_evidence": "独立身份信号不足",
    }
    parts = [
        f"Agent {decisions[verdict.decision]}（{verdict.confidence:.0%}）",
        f"版本关系：{relations[verdict.relation]}",
    ]
    if verdict.evidence:
        parts.append("正向证据：" + "、".join(labels[code] for code in verdict.evidence))
    if verdict.conflicts:
        parts.append("负向/缺失证据：" + "、".join(labels[code] for code in verdict.conflicts))
    if model_rationale.strip():
        parts.append("模型具体理由：" + " ".join(model_rationale.split()))
    if calibration_notes:
        parts.append("系统校准：" + "；".join(calibration_notes))
    return "；".join(parts)[:1800] + "。"

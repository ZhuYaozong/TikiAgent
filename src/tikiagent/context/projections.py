"""模型输入的有界视图；原始报告仍由 History 保存，不能用视图替代门控。"""

from copy import deepcopy


def research_view(payload: dict) -> dict:
    """委派观察保留交付状态、缺口和逐条引用，长正文仍留在 History。"""
    return {
        "delivery_status": payload.get("delivery_status"),
        "stop_reason": payload.get("stop_reason"),
        "finalization_status": payload.get("finalization_status"),
        "findings": [str(f)[:800] for f in payload.get("findings", [])[:12]],
        "finding_citations": deepcopy(payload.get("finding_citations", [])[:12]),
        "sources": [{key: deepcopy(source.get(key)) for key in
                     ("source_id", "observation_id", "title", "url", "published_date", "evidence_kind")}
                    for source in payload.get("sources", [])[:12]],
        "unresolved_questions": [str(q)[:200] for q in payload.get("unresolved_questions", [])[:12]],
        "original_history_ref": payload.get("result_id"),
    }


def verification_facts(payload: dict) -> dict:
    """身份、验收结论和失败分类保持准确，长证据按引用读取。"""
    keys = ("verification_id", "result_id", "handoff_id", "todo_id", "subject_agent", "mode",
            "passed", "failure_category", "retryable", "blocking_reason", "failure_scope", "allowed_actions", "verification_status", "verification_reason", "limitations", "advisory", "hard_blockers")
    facts = {key: deepcopy(payload[key]) for key in keys if key in payload}
    facts["assessments"] = [
        {key: deepcopy(value) for key, value in assessment.items()
         if key not in {"reason", "evidence"}}
        for assessment in payload.get("assessments", [])
    ]
    return facts


def verification_view(payload: dict) -> dict:
    """为委派 Observation 提供简短报告，不重复注入完整证据正文。"""
    view = verification_facts(payload)
    # 失败项的简短原因供重新规划使用；它不属于不可压缩的控制事实。
    view["assessments"] = [
        {**fact, "reason": str(original.get("reason", ""))[:240]}
        for fact, original in zip(view["assessments"], payload.get("assessments", []))
    ]
    for key in ("failures", "recommendation"):
        value = payload.get(key)
        if value is not None:
            view[key] = str(value)[:800]
    view["checks"] = [{"name": c.get("name"), "passed": c.get("passed"), "evidence": str(c.get("evidence", ""))[:240]}
                      for c in payload.get("checks", [])[:24]]
    view["original_history_ref"] = payload.get("verification_id")
    return view

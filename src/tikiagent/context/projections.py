"""模型输入的有界视图；原始报告仍由 History 保存，不能用视图替代门控。"""

from copy import deepcopy


def verification_facts(payload: dict) -> dict:
    """身份、验收结论和失败分类保持准确，长证据按引用读取。"""
    keys = ("verification_id", "result_id", "handoff_id", "todo_id", "subject_agent", "mode",
            "passed", "failure_category", "retryable", "blocking_reason", "failure_scope", "allowed_actions", "verification_status", "advisory", "hard_blockers")
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
    view["original_history_ref"] = payload.get("verification_id")
    return view

"""将执行事实分类，不把所有停止原因都解释为预算问题。"""

from tikiagent.orchestration.contracts import VerificationReport


def execution_failure(handoff, result):
    if getattr(result, "finalization_status", None) in {"failed", "already_consumed"}:
        diagnosis = getattr(result, "finalization_diagnostics", {})
        reason = "已有执行证据，但结果整理未完成：" + diagnosis.get("error_category", "finalization_unavailable")
        report = failure_report(handoff, result, "model_response", reason)
        # 总结错误不是缺少搜索结果，不能借重规划刷新工具或收尾预算。
        return report.model_copy(update={"allowed_actions": ["stop"]})
    reason = getattr(result, "stop_reason", None) or "no_delivery"
    last = result.tool_results[-1] if getattr(result, "tool_results", ()) else {}
    error = last.get("error") or {}
    permission = error.get("code") in {"permission_denied", "approval_rejected", "tool_not_exposed", "workspace_escape"}
    category = ("permission" if permission else "model_response" if reason == "model_response" else
                "budget" if reason in {"max_steps", "tool_budget_exhausted", "context_budget_exhausted", "request_budget_exhausted", "task_web_budget_or_duplicate"} else
                "insufficient_evidence" if reason in {"no_progress", "no_delivery"} else "unknown")
    return failure_report(handoff, result, category, reason)


def failure_report(handoff, result, category, reason):
    return VerificationReport(result_id=result.result_id, handoff_id=handoff.handoff_id,
        subject_agent=handoff.to_agent, todo_id=handoff.todo_id, mode="rules", passed=False,
        checks=[], failures=[reason], evidence=[], recommendation="保留部分成果；依据失败作用域停止或重新规划，不代表产物已被判定错误",
        failure_category=category, retryable=False, blocking_reason=reason,
        verification_status="not_performed", failure_scope="run", allowed_actions=["stop", "replan"] if category != "permission" else ["stop"])

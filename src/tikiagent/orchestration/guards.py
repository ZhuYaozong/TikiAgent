"""Supervisor 的结果身份检查与待办选择。"""

from __future__ import annotations

from tikiagent.context.task_board import next_actionable_todo
from tikiagent.orchestration.contracts import SpecialistName
from tikiagent.orchestration.state import TikiState


def latest_result_is_verified(
    state: TikiState,
    specialist: SpecialistName,
) -> bool:
    """只认可 Specialist 最新 Result 对应的 PASS。"""

    raw_result = state["specialist_results"].get(specialist)
    report = state["specialist_verifications"].get(specialist)
    if raw_result is None or report is None or not report.passed:
        return False
    result_id = raw_result.get("result_id")
    handoff_id = raw_result.get("handoff_id")
    return (
        isinstance(result_id, str)
        and isinstance(handoff_id, str)
        and report.result_id == result_id
        and report.handoff_id == handoff_id
        and report.subject_agent == specialist
    )


def next_required_specialist(
    state: TikiState,
) -> SpecialistName | None:
    """优先按 Task Board 找待办，并兼容没有 Board 的旧工作流。"""

    for specialist in state["required_specialists"]:
        if next_actionable_todo(state["task_board"], specialist) is not None:
            return specialist

    for specialist in state["required_specialists"]:
        if not latest_result_is_verified(state, specialist):
            return specialist
    return None

"""使用结构化输出进行计划、路由和结束保护的 Supervisor。"""

import json
from typing import cast

from tikiagent.context.models import BaseContext
from tikiagent.context.task_board import next_actionable_todo
from tikiagent.llm.models import StructuredModelClient
from tikiagent.orchestration.models import (
    SpecialistName,
    SupervisorDecision,
    SupervisorPlan,
)
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


class SupervisorAgent:
    """模型负责语义规划，程序负责不可绕过的控制流契约。"""

    def __init__(self, model: StructuredModelClient) -> None:
        self.model = model

    def plan(self, task: str) -> SupervisorPlan:
        plan = cast(
            SupervisorPlan,
            self.model.complete_structured(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "你是 TikiAgent Supervisor。判断任务需要"
                            " research_agent、code_agent 或两者。需要当前"
                            "外部信息时先 research 后 code。生成可验证的"
                            "验收标准，不调用工具。"
                        ),
                    },
                    {"role": "user", "content": task},
                ],
                response_type=SupervisorPlan,
            ),
        )
        # 去重同时保留模型规划顺序。
        required = list(dict.fromkeys(plan.required_specialists))
        return plan.model_copy(update={"required_specialists": required})

    def decide(
        self,
        state: TikiState,
        base_context: BaseContext | None = None,
    ) -> SupervisorDecision:
        target = next_required_specialist(state)
        if target is None:
            return SupervisorDecision(
                action="finish",
                target_agent=None,
                instruction="",
                reason="所有必要 Specialist 的最新 Result 均有匹配 PASS",
                context_refs=self._completion_refs(state),
            )
        if state["delegation_count"] >= state["max_delegations"]:
            return SupervisorDecision(
                action="stop",
                target_agent=None,
                instruction="",
                reason="需要继续委派，但已达到 max_delegations",
            )

        decision = cast(
            SupervisorDecision,
            self.model.complete_structured(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "你是 TikiAgent Supervisor，只生成下一次委派"
                            "指令，不直接搜索、写文件或验证。系统已经确定"
                            f"下一目标必须是 {target}，action 必须是 delegate。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            base_context.render()
                            if base_context is not None
                            else self._decision_context(state)
                        ),
                    },
                ],
                response_type=SupervisorDecision,
            ),
        )
        return SupervisorDecision(
            action="delegate",
            target_agent=target,
            instruction=(
                decision.instruction.strip()
                or self._default_instruction(target, state)
            ),
            reason=decision.reason,
            context_refs=self._context_refs(target, state),
        )

    @staticmethod
    def _context_refs(
        target: SpecialistName,
        state: TikiState,
    ) -> list[str]:
        refs: list[str] = []
        if (
            target == "code_agent"
            and latest_result_is_verified(state, "research_agent")
        ):
            research_result = state["specialist_results"].get(
                "research_agent"
            )
            if research_result is not None:
                result_id = research_result.get("result_id")
                if isinstance(result_id, str):
                    refs.append(result_id)
        report = state["specialist_verifications"].get(target)
        if report is not None and not latest_result_is_verified(state, target):
            raw_result = state["specialist_results"].get(target)
            result_id = raw_result.get("result_id") if raw_result else None
            if isinstance(result_id, str):
                refs.append(result_id)
            refs.append(report.verification_id)
        return list(dict.fromkeys(refs))

    @staticmethod
    def _completion_refs(state: TikiState) -> list[str]:
        refs: list[str] = []
        for specialist in state["required_specialists"]:
            result = state["specialist_results"].get(specialist)
            result_id = result.get("result_id") if result else None
            if isinstance(result_id, str):
                refs.append(result_id)
        return refs

    @staticmethod
    def _default_instruction(
        target: SpecialistName,
        state: TikiState,
    ) -> str:
        todo = next_actionable_todo(state["task_board"], target)
        if todo is not None and todo.status == "pending":
            return todo.description
        if target == "research_agent":
            return "调研任务所需信息，保留真实 Web 来源和搜索证据"
        report = state["specialist_verifications"].get(target)
        if report is not None and not report.passed:
            return "根据 VerificationReport 修复失败项并重新执行环境检查"
        return "根据验收标准和允许的结构化上下文完成代码与文件交付"

    @staticmethod
    def _decision_context(state: TikiState) -> str:
        plan = state["supervisor_plan"]
        reports = {
            name: report.model_dump(mode="json")
            for name, report in state["specialist_verifications"].items()
        }
        return (
            f"任务：{state['task']}\n"
            f"计划：{plan.model_dump_json() if plan else 'null'}\n"
            f"已有 Result：{list(state['specialist_results'])}\n"
            "最新验证："
            f"{json.dumps(reports, ensure_ascii=False)}"
        )

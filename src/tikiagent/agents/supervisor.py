"""生成计划和委派决策的 Supervisor。"""

from __future__ import annotations

from typing import cast
import json

from tikiagent.context.memory.models import LocalMemory
from tikiagent.context.models import BaseContext, WorkingMemory
from tikiagent.context.preparation import ContextRuntime
from tikiagent.context.task_board import next_actionable_todo
from tikiagent.orchestration.contracts import (
    SpecialistName,
    SupervisorDecision,
    SupervisorPlan,
)
from tikiagent.orchestration.guards import (
    latest_result_is_verified,
    next_required_specialist,
)
from tikiagent.orchestration.state import TikiState
from tikiagent.providers.llm.models import StructuredModelClient
from tikiagent.tools.registry import ToolRegistry


class SupervisorAgent:
    """模型负责语义规划，程序负责不可绕过的控制流契约。"""

    def __init__(
        self,
        model: StructuredModelClient,
        context_runtime: ContextRuntime | None = None,
    ) -> None:
        self.model = model
        self.context_runtime = context_runtime or ContextRuntime()

    def plan(self, task: str) -> SupervisorPlan:
        context = BaseContext(
            agent="supervisor",
            working_memory=WorkingMemory(
                task=task,
                phase="planning",
                instruction=(
                    "判断需要哪些 Specialist，并生成可验证的验收标准。"
                ),
            ),
        )
        prepared = self.context_runtime.prepare(
            base_context=context,
            local_memory=LocalMemory(),
            registry=ToolRegistry(),
            response_type=SupervisorPlan,
        )
        plan = cast(
            SupervisorPlan,
            self.model.complete_structured(
                messages=prepared.messages,
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

        decision_context = base_context or BaseContext(
            agent="supervisor",
            working_memory=WorkingMemory(
                task=state["task"],
                phase="routing",
                instruction=self._decision_context(state),
                acceptance_criteria=state["acceptance_criteria"],
                todos=list(state["task_board"].items.values()),
            ),
        )
        decision_context = decision_context.model_copy(
            update={
                "working_memory": decision_context.working_memory.model_copy(
                    update={
                        "instruction": (
                            f"{decision_context.working_memory.instruction}\n"
                            f"程序已确定下一目标必须是 {target}，"
                            "action 必须是 delegate。"
                        )
                    }
                )
            }
        )
        prepared = self.context_runtime.prepare(
            base_context=decision_context,
            local_memory=LocalMemory(),
            registry=ToolRegistry(),
            response_type=SupervisorDecision,
        )
        decision = cast(
            SupervisorDecision,
            self.model.complete_structured(
                messages=prepared.messages,
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

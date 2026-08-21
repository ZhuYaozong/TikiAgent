"""Planner → Actor → Verifier → Planner 控制闭环。"""

from collections.abc import Iterator
from typing import Any, Literal, Protocol, cast

from langgraph.graph import END, START, StateGraph

from tikiagent.orchestration.models import (
    ActorResult,
    Plan,
    PlannerDecision,
    PlanningResult,
    VerificationReport,
)
from tikiagent.orchestration.state import (
    TikiState,
    create_plan_verify_state,
)


class TaskPlanner(Protocol):
    """PlanVerifyWorkflow 对 Planner 的最小依赖。"""

    def plan(self, task: str) -> PlanningResult: ...

    def decide(
        self,
        *,
        task: str,
        plan: Plan,
        report: VerificationReport,
        attempts: int,
        max_attempts: int,
    ) -> PlannerDecision: ...


class TaskActor(Protocol):
    """PlanVerifyWorkflow 对执行 Agent 的最小依赖。"""

    max_steps: int

    def execute(
        self,
        *,
        instruction: str,
        plan: Plan,
        acceptance_criteria: list[str],
    ) -> ActorResult: ...


class TaskVerifier(Protocol):
    """PlanVerifyWorkflow 对环境验证器的最小依赖。"""

    def verify(
        self,
        *,
        task: str,
        plan: Plan,
        actor_result: ActorResult,
    ) -> VerificationReport: ...


class PlanVerifyWorkflow:
    """显式编排计划、执行、验证、重试和结束。"""

    def __init__(
        self,
        *,
        planner: TaskPlanner,
        actor: TaskActor,
        verifier: TaskVerifier,
        workspace_id: str,
        max_attempts: int = 3,
        recursion_limit: int | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts 必须大于 0")
        if actor.max_steps < 1:
            raise ValueError("max_actor_steps 必须大于 0")
        if recursion_limit is not None and recursion_limit < 1:
            raise ValueError("recursion_limit 必须大于 0")

        self.planner = planner
        self.actor = actor
        self.verifier = verifier
        self.workspace_id = workspace_id
        self.max_attempts = max_attempts
        self.max_actor_steps = actor.max_steps
        self.recursion_limit = (
            recursion_limit
            if recursion_limit is not None
            else max_attempts * 3 + 4
        )
        self.graph = self._build_graph()

    def _planner_node(self, state: TikiState) -> dict[str, Any]:
        """首次创建计划，之后根据 VerificationReport 决策。"""

        if state["plan"] is None:
            planning = self.planner.plan(state["task"])
            decision = self._guard_initial_decision(planning)
            return {
                "plan": planning.plan,
                "acceptance_criteria": (
                    planning.plan.acceptance_criteria
                ),
                "planner_decision": decision,
                "actor_instruction": decision.instruction,
                "actor_result": None,
                "verification_report": None,
                "attempts": 1,
                "status": "executing",
            }

        report = state["verification_report"]
        if report is None:
            raise RuntimeError("Planner 缺少 VerificationReport")

        decision = self.planner.decide(
            task=state["task"],
            plan=state["plan"],
            report=report,
            attempts=state["attempts"],
            max_attempts=state["max_attempts"],
        )
        decision = self._guard_verification_decision(state, decision)

        if decision.action in {"execute", "retry"}:
            return {
                "planner_decision": decision,
                "actor_instruction": decision.instruction,
                "actor_result": None,
                "verification_report": None,
                "attempts": state["attempts"] + 1,
                "status": "executing",
            }

        if decision.action == "finish":
            return {
                "planner_decision": decision,
                "status": "completed",
                "final_result": self._completion_text(state, decision),
            }

        status = (
            "max_attempts"
            if state["attempts"] >= state["max_attempts"]
            else "failed"
        )
        return {
            "planner_decision": decision,
            "status": status,
            "final_result": self._completion_text(state, decision),
        }

    def _actor_node(self, state: TikiState) -> dict[str, Any]:
        """执行一轮隔离的 ReAct Actor。"""

        plan = state["plan"]
        if plan is None:
            raise RuntimeError("Actor 缺少 Plan")

        result = self.actor.execute(
            instruction=state["actor_instruction"],
            plan=plan,
            acceptance_criteria=state["acceptance_criteria"],
        )
        return {
            "actor_result": result,
            "tool_results": list(result.tool_results),
            "step_count": state["step_count"] + result.steps,
            "status": "verifying",
        }

    def _verifier_node(self, state: TikiState) -> dict[str, Any]:
        """只生成报告；固定回 Planner，不在此处修复或结束。"""

        plan = state["plan"]
        actor_result = state["actor_result"]
        if plan is None or actor_result is None:
            raise RuntimeError("Verifier 缺少 Plan 或 ActorResult")

        report = self.verifier.verify(
            task=state["task"],
            plan=plan,
            actor_result=actor_result,
        )
        return {
            "verification_report": report,
            "status": "planning",
        }

    @staticmethod
    def _guard_initial_decision(
        planning: PlanningResult,
    ) -> PlannerDecision:
        decision = planning.decision
        if decision.action == "execute" and decision.instruction:
            return decision

        return PlannerDecision(
            action="execute",
            instruction=(
                decision.instruction
                or "按照 Plan 执行并收集环境验证证据"
            ),
            reason=(
                "首次规划必须先执行，不能直接结束。"
                f"Planner 原理由：{decision.reason}"
            ),
        )

    @staticmethod
    def _guard_verification_decision(
        state: TikiState,
        decision: PlannerDecision,
    ) -> PlannerDecision:
        report = state["verification_report"]
        if report is None:
            raise RuntimeError("缺少 VerificationReport")

        if (
            not report.passed
            and state["attempts"] >= state["max_attempts"]
        ):
            return PlannerDecision(
                action="stop",
                instruction="",
                reason="达到 max_attempts，禁止继续执行",
            )

        if not report.passed and decision.action == "finish":
            return PlannerDecision(
                action="retry",
                instruction=(
                    decision.instruction
                    or "根据 VerificationReport 修复失败项"
                ),
                reason=(
                    "Verifier 未通过，禁止 FINISH。"
                    f"Planner 原理由：{decision.reason}"
                ),
            )

        if (
            decision.action in {"execute", "retry"}
            and not decision.instruction
        ):
            return PlannerDecision(
                action=decision.action,
                instruction="根据 VerificationReport 继续执行",
                reason=decision.reason,
            )

        return decision

    @staticmethod
    def _completion_text(
        state: TikiState,
        decision: PlannerDecision,
    ) -> str:
        report = state["verification_report"]
        report_text = (
            report.model_dump_json(indent=2)
            if report is not None
            else "null"
        )
        return (
            f"Planner action={decision.action}: {decision.reason}\n"
            f"VerificationReport:\n{report_text}"
        )

    @staticmethod
    def _route_after_planner(
        state: TikiState,
    ) -> Literal["actor", "end"]:
        decision = state["planner_decision"]
        if decision and decision.action in {"execute", "retry"}:
            return "actor"
        return "end"

    def _build_graph(self):
        builder = StateGraph(TikiState)
        builder.add_node("planner", self._planner_node)
        builder.add_node("actor", self._actor_node)
        builder.add_node("verifier", self._verifier_node)
        builder.add_edge(START, "planner")
        builder.add_conditional_edges(
            "planner",
            self._route_after_planner,
            {"actor": "actor", "end": END},
        )
        builder.add_edge("actor", "verifier")

        # PASS 和 FAIL 都必须由 Planner 做下一步决定。
        builder.add_edge("verifier", "planner")
        return builder.compile()

    def initial_state(
        self,
        task: str,
        *,
        session_id: str | None = None,
    ) -> TikiState:
        return create_plan_verify_state(
            task=task,
            workspace_id=self.workspace_id,
            max_steps=self.max_actor_steps,
            max_attempts=self.max_attempts,
            session_id=session_id,
        )

    def invoke(
        self,
        task: str,
        *,
        session_id: str | None = None,
    ) -> TikiState:
        result = self.graph.invoke(
            self.initial_state(task, session_id=session_id),
            config={"recursion_limit": self.recursion_limit},
        )
        return cast(TikiState, result)

    def stream(
        self,
        task: str,
        *,
        session_id: str | None = None,
    ) -> Iterator[TikiState]:
        snapshots = self.graph.stream(
            self.initial_state(task, session_id=session_id),
            config={"recursion_limit": self.recursion_limit},
            stream_mode="values",
        )
        for snapshot in snapshots:
            yield cast(TikiState, snapshot)

"""Supervisor → Specialist → Verification Gate 主工作流。"""

from collections.abc import Iterator
from typing import Any, Literal, Protocol, cast

from langgraph.graph import END, START, StateGraph

from tikiagent.agents.supervisor import latest_result_is_verified
from tikiagent.orchestration.models import (
    CodeResult,
    Handoff,
    ResearchResult,
    SupervisorDecision,
    SupervisorPlan,
    VerificationReport,
)
from tikiagent.orchestration.state import TikiState, create_multi_agent_state


class Supervisor(Protocol):
    def plan(self, task: str) -> SupervisorPlan: ...

    def decide(self, state: TikiState) -> SupervisorDecision: ...


class ResearchSpecialist(Protocol):
    def run(self, handoff: Handoff) -> ResearchResult: ...


class CodeSpecialist(Protocol):
    max_steps: int

    def run(
        self,
        *,
        handoff: Handoff,
        context_payload: dict[str, Any],
    ) -> CodeResult: ...


class ResultVerificationGate(Protocol):
    def verify(
        self,
        *,
        handoff: Handoff,
        raw_result: dict[str, Any],
        specialist_results: dict[str, dict[str, Any]],
    ) -> VerificationReport: ...


class MultiAgentWorkflow:
    """主架构采用 Graph Routing，不把 Specialist 注册为工具。"""

    def __init__(
        self,
        *,
        supervisor: Supervisor,
        research_agent: ResearchSpecialist | None,
        code_agent: CodeSpecialist | None,
        verification_gate: ResultVerificationGate,
        workspace_id: str,
        max_delegations: int = 4,
        recursion_limit: int = 30,
    ) -> None:
        if max_delegations < 1:
            raise ValueError("max_delegations 必须大于 0")
        if recursion_limit < 1:
            raise ValueError("recursion_limit 必须大于 0")
        self.supervisor = supervisor
        self.research_agent = research_agent
        self.code_agent = code_agent
        self.verification_gate = verification_gate
        self.workspace_id = workspace_id
        self.max_delegations = max_delegations
        self.recursion_limit = recursion_limit
        self.max_steps = code_agent.max_steps if code_agent is not None else 1
        self.graph = self._build_graph()

    def _supervisor_node(self, state: TikiState) -> dict[str, Any]:
        updates: dict[str, Any] = {}
        decision_state = state
        if state["supervisor_plan"] is None:
            plan = self.supervisor.plan(state["task"])
            updates = {
                "supervisor_plan": plan,
                "required_specialists": plan.required_specialists,
                "acceptance_criteria": plan.acceptance_criteria,
                "recent_events": ["supervisor: plan"],
            }
            decision_state = cast(TikiState, {**state, **updates})

        decision = self.supervisor.decide(decision_state)
        if decision.action == "finish":
            unverified = [
                specialist
                for specialist in decision_state["required_specialists"]
                if not latest_result_is_verified(
                    decision_state,
                    specialist,
                )
            ]
            if unverified:
                rejected = SupervisorDecision(
                    action="stop",
                    target_agent=None,
                    instruction="",
                    reason=(
                        "Graph 拒绝 FINISH：必要 Specialist 的最新 Result "
                        f"尚无匹配 PASS：{unverified}"
                    ),
                )
                return {
                    **updates,
                    "supervisor_decision": rejected,
                    "current_agent": "supervisor",
                    "status": "stopped",
                    "final_result": rejected.reason,
                    "recent_events": ["supervisor: finish rejected"],
                }
            return {
                **updates,
                "supervisor_decision": decision,
                "current_agent": "supervisor",
                "status": "completed",
                "final_result": self._completion_text(decision_state),
                "recent_events": ["supervisor: finish"],
            }
        if decision.action == "stop":
            return {
                **updates,
                "supervisor_decision": decision,
                "current_agent": "supervisor",
                "status": "stopped",
                "final_result": decision.reason,
                "recent_events": ["supervisor: stop"],
            }

        target = decision.target_agent
        if target is None:
            raise RuntimeError("delegate 决策缺少 target_agent")
        if target == "research_agent" and self.research_agent is None:
            raise RuntimeError("任务需要 ResearchAgent，但应用未配置")
        if target == "code_agent" and self.code_agent is None:
            raise RuntimeError("任务需要 CodeAgent，但应用未配置")

        handoff = Handoff(
            from_agent="supervisor",
            to_agent=target,
            instruction=decision.instruction,
            context_refs=decision.context_refs,
        )
        return {
            **updates,
            "supervisor_decision": decision,
            "current_agent": target,
            "latest_handoff": handoff,
            "delegation_count": state["delegation_count"] + 1,
            "status": "delegating",
            "recent_events": [f"supervisor: delegate {target}"],
        }

    def _research_node(self, state: TikiState) -> dict[str, Any]:
        handoff = self._require_pending_handoff(state, "research_agent")
        if self.research_agent is None:  # pragma: no cover - 路由前已保护
            raise RuntimeError("ResearchAgent 未配置")
        result = self.research_agent.run(handoff)
        completed = handoff.model_copy(
            update={"result_id": result.result_id, "status": "completed"}
        )
        return {
            "current_agent": "verification_gate",
            "latest_handoff": completed,
            "recent_handoffs": [completed],
            "specialist_results": {
                "research_agent": result.model_dump(mode="json")
            },
            "status": "validating",
            "recent_events": ["research_agent: result"],
        }

    def _code_node(self, state: TikiState) -> dict[str, Any]:
        handoff = self._require_pending_handoff(state, "code_agent")
        if self.code_agent is None:  # pragma: no cover - 路由前已保护
            raise RuntimeError("CodeAgent 未配置")
        result = self.code_agent.run(
            handoff=handoff,
            context_payload=self._context_payload(state, handoff),
        )
        completed = handoff.model_copy(
            update={"result_id": result.result_id, "status": "completed"}
        )
        return {
            "current_agent": "verification_gate",
            "latest_handoff": completed,
            "recent_handoffs": [completed],
            "specialist_results": {
                "code_agent": result.model_dump(mode="json")
            },
            "status": "validating",
            "recent_events": ["code_agent: result"],
        }

    def _verification_node(self, state: TikiState) -> dict[str, Any]:
        handoff = state["latest_handoff"]
        if handoff is None or handoff.status != "completed":
            raise RuntimeError("Verification Gate 缺少 completed Handoff")
        raw_result = state["specialist_results"].get(handoff.to_agent)
        if raw_result is None:
            raise RuntimeError("Verification Gate 缺少 Specialist Result")
        report = self.verification_gate.verify(
            handoff=handoff,
            raw_result=raw_result,
            specialist_results=state["specialist_results"],
        )
        return {
            "current_agent": "supervisor",
            "verification_report": report,
            "specialist_verifications": {handoff.to_agent: report},
            "status": "running",
            "recent_events": [
                f"verification_gate: {handoff.to_agent} passed={report.passed}"
            ],
        }

    @staticmethod
    def _route_after_supervisor(
        state: TikiState,
    ) -> Literal["research", "code", "end"]:
        decision = state["supervisor_decision"]
        if decision is None or decision.action != "delegate":
            return "end"
        if decision.target_agent == "research_agent":
            return "research"
        if decision.target_agent == "code_agent":
            return "code"
        raise RuntimeError("未知 target_agent")

    def _build_graph(self):
        builder = StateGraph(TikiState)
        builder.add_node("supervisor", self._supervisor_node)
        builder.add_node("research_agent", self._research_node)
        builder.add_node("code_agent", self._code_node)
        builder.add_node("verification_gate", self._verification_node)
        builder.add_edge(START, "supervisor")
        builder.add_conditional_edges(
            "supervisor",
            self._route_after_supervisor,
            {"research": "research_agent", "code": "code_agent", "end": END},
        )
        builder.add_edge("research_agent", "verification_gate")
        builder.add_edge("code_agent", "verification_gate")
        builder.add_edge("verification_gate", "supervisor")
        return builder.compile()

    @staticmethod
    def _require_pending_handoff(
        state: TikiState,
        expected_agent: str,
    ) -> Handoff:
        handoff = state["latest_handoff"]
        if (
            handoff is None
            or handoff.status != "pending"
            or handoff.to_agent != expected_agent
        ):
            raise RuntimeError(f"{expected_agent} 缺少有效 pending Handoff")
        return handoff

    @staticmethod
    def _context_payload(
        state: TikiState,
        handoff: Handoff,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if "acceptance_criteria" in handoff.context_refs:
            payload["acceptance_criteria"] = state["acceptance_criteria"]
        if "research_result" in handoff.context_refs:
            payload["research_result"] = state["specialist_results"].get(
                "research_agent"
            )
        if "verification_report" in handoff.context_refs:
            report = state["specialist_verifications"].get(
                handoff.to_agent
            )
            payload["verification_report"] = (
                report.model_dump(mode="json") if report else None
            )
        return payload

    @staticmethod
    def _completion_text(state: TikiState) -> str:
        result_ids = {
            name: result.get("result_id")
            for name, result in state["specialist_results"].items()
            if name in state["required_specialists"]
        }
        return f"任务完成；最新已验证 Result：{result_ids}"

    def initial_state(
        self,
        task: str,
        *,
        session_id: str | None = None,
    ) -> TikiState:
        return create_multi_agent_state(
            task=task,
            workspace_id=self.workspace_id,
            max_steps=self.max_steps,
            max_delegations=self.max_delegations,
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

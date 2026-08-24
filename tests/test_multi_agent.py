"""正式 Multi-Agent Graph 路由与 Verification Gate 闭环测试。"""

from tikiagent.agents.supervisor import SupervisorAgent, next_required_specialist
from tikiagent.context import BaseContext, HistoryRecord
from tikiagent.orchestration.models import (
    CodeResult,
    Handoff,
    ResearchObservation,
    ResearchResult,
    ResearchSource,
    SupervisorDecision,
    SupervisorPlan,
    VerificationReport,
)
from tikiagent.orchestration.multi_agent import MultiAgentWorkflow
from tikiagent.orchestration.verification_gate import VerificationGate


class StructuredModel:
    def __init__(self, required: list[str]) -> None:
        self.required = required

    def complete_structured(self, messages, response_type):
        del messages
        if response_type.__name__ == "SupervisorPlan":
            return response_type.model_validate(
                {
                    "goal": "task",
                    "required_specialists": self.required,
                    "acceptance_criteria": ["verified"],
                }
            )
        return response_type.model_validate(
            {
                "action": "delegate",
                "target_agent": self.required[0],
                "instruction": "execute",
                "reason": "required",
            }
        )


class ResearchStub:
    def __init__(self) -> None:
        self.calls = 0
        self.contexts: list[BaseContext] = []

    def run(
        self,
        handoff: Handoff,
        base_context: BaseContext,
    ) -> ResearchResult:
        self.calls += 1
        self.contexts.append(base_context)
        return ResearchResult(
            result_id=f"research-{self.calls}",
            handoff_id=handoff.handoff_id,
            summary="research",
            findings=["finding"],
            sources=[
                ResearchSource(
                    observation_id="search-1",
                    title="source",
                    url="https://example.com/source",
                    snippet="snippet",
                )
            ],
            observations=[
                ResearchObservation(
                    observation_id="search-1",
                    query="agent",
                    urls=["https://example.com/source"],
                )
            ],
            queries=["agent"],
        )


class CodeStub:
    max_steps = 4

    def __init__(self) -> None:
        self.calls = 0
        self.contexts: list[BaseContext] = []

    def run(self, *, handoff: Handoff, base_context: BaseContext):
        self.calls += 1
        self.contexts.append(base_context)
        return CodeResult(
            result_id=f"code-{self.calls}",
            handoff_id=handoff.handoff_id,
            summary="code",
            completed=True,
            steps=1,
            changed_files=["comparison.html"],
            context_refs_used=handoff.context_refs,
        )


class LinkedVerifier:
    def __init__(self, agent: str, passes: list[bool]) -> None:
        self.agent = agent
        self.passes = passes
        self.calls = 0

    def verify(self, *, handoff, result, specialist_results):
        del specialist_results
        passed = self.passes[min(self.calls, len(self.passes) - 1)]
        self.calls += 1
        return VerificationReport.model_validate(
            {
                "result_id": result.result_id,
                "handoff_id": handoff.handoff_id,
                "subject_agent": self.agent,
                "mode": "rules" if self.agent == "research_agent" else "environment",
                "passed": passed,
                "checks": [],
                "failures": [] if passed else ["injected failure"],
                "evidence": [],
                "recommendation": "continue",
            }
        )


def workflow(
    required,
    *,
    research_passes=None,
    code_passes=None,
    max_delegations=4,
):
    research = ResearchStub()
    code = CodeStub()
    research_verifier = LinkedVerifier(
        "research_agent",
        research_passes or [True],
    )
    code_verifier = LinkedVerifier(
        "code_agent",
        code_passes or [True],
    )
    value = MultiAgentWorkflow(
        supervisor=SupervisorAgent(StructuredModel(required)),
        research_agent=research,
        code_agent=code,
        verification_gate=VerificationGate(
            research_verifier=research_verifier,
            code_verifier=code_verifier,
        ),
        workspace_id="workspace",
        max_delegations=max_delegations,
    )
    return value, research, code, research_verifier, code_verifier


def test_research_only_must_pass_verification_gate() -> None:
    value, research, code, research_gate, code_gate = workflow(
        ["research_agent"]
    )

    state = value.invoke("research")

    assert state["status"] == "completed"
    assert research.calls == 1
    assert code.calls == 0
    assert research_gate.calls == 1
    assert code_gate.calls == 0
    assert state["specialist_verifications"]["research_agent"].passed
    assert research.contexts[0].agent == "research_agent"
    assert all(
        item.owner == "research_agent"
        for item in research.contexts[0].working_memory.todos
    )


def test_hybrid_passes_gate_after_each_specialist() -> None:
    value, research, code, research_gate, code_gate = workflow(
        ["research_agent", "code_agent"]
    )

    state = value.invoke("hybrid")

    assert state["status"] == "completed"
    assert research.calls == 1
    assert code.calls == 1
    assert research_gate.calls == 1
    assert code_gate.calls == 1
    history_ids = {
        item.record_id
        for item in code.contexts[0].working_memory.relevant_history
    }
    assert "research-1" in history_ids
    history = value.history_for(state)
    assert [item.record_type for item in history] == [
        "handoff",
        "result",
        "verification",
        "handoff",
        "result",
        "verification",
        "result",
    ]
    assert history[-1].record_id == state["final_result_id"]
    assert state["finalization_report"] is not None
    assert state["finalization_report"].already_finalized is False
    assert state["history_cursor"] == len(history)
    assert all(
        item.status == "completed"
        for item in state["task_board"].items.values()
    )


def test_explicit_session_result_reaches_specialist_without_old_messages() -> None:
    value, _, code, _, _ = workflow(["code_agent"])
    value.history_store.append(
        HistoryRecord(
            record_id="final:previous-task",
            task_id="previous-task",
            session_id="session-1",
            record_type="result",
            producer="supervisor",
            summary="上一轮已验证调研结果",
        )
    )

    state = value.invoke(
        "根据刚才结果生成网页",
        session_id="session-1",
        task_id="current-task",
        session_context_refs=["final:previous-task"],
    )

    assert state["status"] == "completed"
    assert "final:previous-task" in code.contexts[0].working_memory.protected_refs
    history_ids = [
        item.record_id for item in code.contexts[0].working_memory.relevant_history
    ]
    assert "final:previous-task" in history_ids
    assert all(item.record_type != "note" for item in code.contexts[0].working_memory.relevant_history)


def test_failed_code_result_retries_and_only_latest_pass_finishes() -> None:
    value, _, code, _, code_gate = workflow(
        ["code_agent"],
        code_passes=[False, True],
    )

    state = value.invoke("coding")

    assert state["status"] == "completed"
    assert code.calls == 2
    assert code_gate.calls == 2
    latest = state["specialist_results"]["code_agent"]
    report = state["specialist_verifications"]["code_agent"]
    assert latest["result_id"] == "code-2"
    assert report.result_id == "code-2"
    assert report.passed is True
    retry_history = code.contexts[1].working_memory.relevant_history
    assert code.contexts[0].working_memory.phase == "coding"
    assert code.contexts[1].working_memory.phase == "debugging"
    assert "code-1" in {item.record_id for item in retry_history}
    assert any(item.record_type == "verification" for item in retry_history)
    todo = next(iter(state["task_board"].items.values()))
    assert todo.attempts == 2
    assert todo.status == "completed"


def test_finalization_node_replay_does_not_duplicate_final_result() -> None:
    value, _, _, _, _ = workflow(["code_agent"])
    state = value.invoke("coding")
    history_size = len(value.history_for(state))
    replay_state = {**state, "status": "finalizing"}

    updates = value._finalization_node(replay_state)

    assert updates["finalization_report"].already_finalized is True
    assert len(value.history_for(state)) == history_size


def test_failed_research_result_receives_latest_verification_on_retry() -> None:
    value, research, _, research_gate, _ = workflow(
        ["research_agent"],
        research_passes=[False, True],
    )

    state = value.invoke("research")

    retry_history = research.contexts[1].working_memory.relevant_history
    assert state["status"] == "completed"
    assert research.calls == 2
    assert research_gate.calls == 2
    assert "research-1" in {item.record_id for item in retry_history}
    assert any(item.record_type == "verification" for item in retry_history)


def test_failure_stops_when_new_delegation_would_exceed_limit() -> None:
    value, _, code, _, _ = workflow(
        ["code_agent"],
        code_passes=[False],
        max_delegations=1,
    )

    state = value.invoke("coding")

    assert state["status"] == "stopped"
    assert code.calls == 1
    assert state["delegation_count"] == 1


def test_graph_uses_explicit_verification_gate_node() -> None:
    value, *_ = workflow(["research_agent"])

    nodes = set(value.graph.get_graph().nodes)
    assert {"supervisor", "research_agent", "code_agent", "verification_gate"} <= nodes


class MultipleCodeTodoSupervisor:
    """测试 TaskBoard 能让同一 Specialist 连续完成多项工作。"""

    def plan(self, task: str) -> SupervisorPlan:
        del task
        return SupervisorPlan(
            goal="two code todos",
            required_specialists=["code_agent", "code_agent"],
            acceptance_criteria=["both verified"],
        )

    def decide(self, state, base_context) -> SupervisorDecision:
        del base_context
        target = next_required_specialist(state)
        if target is None:
            return SupervisorDecision(
                action="finish",
                target_agent=None,
                instruction="",
                reason="all todos complete",
            )
        return SupervisorDecision(
            action="delegate",
            target_agent=target,
            instruction="execute next todo",
            reason="pending todo",
        )


def test_same_agent_multiple_todos_each_require_result_and_verification() -> None:
    code = CodeStub()
    gate = LinkedVerifier("code_agent", [True, True])
    value = MultiAgentWorkflow(
        supervisor=MultipleCodeTodoSupervisor(),
        research_agent=ResearchStub(),
        code_agent=code,
        verification_gate=VerificationGate(
            research_verifier=LinkedVerifier("research_agent", [True]),
            code_verifier=gate,
        ),
        workspace_id="workspace",
    )

    state = value.invoke("two coding tasks")

    code_todos = [
        item
        for item in state["task_board"].items.values()
        if item.owner == "code_agent"
    ]
    assert state["status"] == "completed"
    assert code.calls == 2
    assert gate.calls == 2
    assert len(code_todos) == 2
    assert all(item.status == "completed" for item in code_todos)
    assert {item.result_id for item in code_todos} == {"code-1", "code-2"}


class UnsafeFinishingSupervisor:
    def plan(self, task: str) -> SupervisorPlan:
        del task
        return SupervisorPlan(
            goal="research",
            required_specialists=["research_agent"],
            acceptance_criteria=["verified"],
        )

    def decide(self, state, base_context) -> SupervisorDecision:
        del state, base_context
        return SupervisorDecision(
            action="finish",
            target_agent=None,
            instruction="",
            reason="unsafe early finish",
        )


def test_graph_rejects_early_finish_from_replaced_supervisor() -> None:
    research = ResearchStub()
    code = CodeStub()
    value = MultiAgentWorkflow(
        supervisor=UnsafeFinishingSupervisor(),
        research_agent=research,
        code_agent=code,
        verification_gate=VerificationGate(
            research_verifier=LinkedVerifier("research_agent", [True]),
            code_verifier=LinkedVerifier("code_agent", [True]),
        ),
        workspace_id="workspace",
    )

    state = value.invoke("research")

    assert state["status"] == "stopped"
    assert research.calls == 0
    assert "拒绝 FINISH" in state["final_result"]
    assert state["finalization_report"] is None
    assert value.history_for(state) == []

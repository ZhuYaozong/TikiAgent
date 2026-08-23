"""正式 Multi-Agent Graph 路由与 Verification Gate 闭环测试。"""

from typing import Any

from tikiagent.agents.supervisor import SupervisorAgent
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

    def run(self, handoff: Handoff) -> ResearchResult:
        self.calls += 1
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
        self.payloads: list[dict[str, Any]] = []

    def run(self, *, handoff: Handoff, context_payload):
        self.calls += 1
        self.payloads.append(context_payload)
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


def workflow(required, *, code_passes=None, max_delegations=4):
    research = ResearchStub()
    code = CodeStub()
    research_verifier = LinkedVerifier("research_agent", [True])
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
    assert "research_result" in code.payloads[0]
    assert len(state["recent_handoffs"]) == 2


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
    assert "verification_report" in code.payloads[1]


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


class UnsafeFinishingSupervisor:
    def plan(self, task: str) -> SupervisorPlan:
        del task
        return SupervisorPlan(
            goal="research",
            required_specialists=["research_agent"],
            acceptance_criteria=["verified"],
        )

    def decide(self, state) -> SupervisorDecision:
        del state
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

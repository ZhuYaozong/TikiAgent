"""Supervisor 规划、路由和最新 Result 验证保护测试。"""

from tikiagent.agents.supervisor import (
    SupervisorAgent,
    latest_result_is_verified,
)
from tikiagent.orchestration.models import VerificationReport
from tikiagent.orchestration.state import create_multi_agent_state


class StructuredModel:
    def __init__(self, required=None) -> None:
        self.required = required or ["research_agent", "code_agent"]

    def complete_structured(self, messages, response_type):
        del messages
        if response_type.__name__ == "SupervisorPlan":
            return response_type.model_validate(
                {
                    "goal": "hybrid",
                    "required_specialists": self.required,
                    "acceptance_criteria": ["verified"],
                }
            )
        return response_type.model_validate(
            {
                "action": "delegate",
                "target_agent": "research_agent",
                "instruction": "execute specialist work",
                "reason": "required",
                "context_refs": ["untrusted-model-ref"],
            }
        )


def state():
    value = create_multi_agent_state(
        task="hybrid",
        workspace_id="workspace",
        max_steps=8,
        max_delegations=4,
    )
    plan = SupervisorAgent(StructuredModel()).plan("hybrid")
    value["supervisor_plan"] = plan
    value["required_specialists"] = plan.required_specialists
    value["acceptance_criteria"] = plan.acceptance_criteria
    return value


def report(*, result_id: str, handoff_id: str, agent: str, passed=True):
    return VerificationReport.model_validate(
        {
            "result_id": result_id,
            "handoff_id": handoff_id,
            "subject_agent": agent,
            "mode": "rules" if agent == "research_agent" else "environment",
            "passed": passed,
            "checks": [],
            "failures": [] if passed else ["failed"],
            "evidence": [],
            "recommendation": "continue",
        }
    )


def test_old_pass_does_not_verify_new_result() -> None:
    value = state()
    value["specialist_results"] = {
        "research_agent": {
            "result_id": "new-result",
            "handoff_id": "new-handoff",
        }
    }
    value["specialist_verifications"] = {
        "research_agent": report(
            result_id="old-result",
            handoff_id="old-handoff",
            agent="research_agent",
        )
    }

    assert latest_result_is_verified(value, "research_agent") is False
    decision = SupervisorAgent(StructuredModel()).decide(value)
    assert decision.action == "delegate"
    assert decision.target_agent == "research_agent"
    assert "verification_report" in decision.context_refs


def test_pass_for_other_agent_cannot_verify_result() -> None:
    value = state()
    value["specialist_results"] = {
        "research_agent": {
            "result_id": "result-1",
            "handoff_id": "handoff-1",
        }
    }
    value["specialist_verifications"] = {
        "research_agent": report(
            result_id="result-1",
            handoff_id="handoff-1",
            agent="code_agent",
        )
    }

    assert latest_result_is_verified(value, "research_agent") is False


def test_finish_requires_every_required_latest_result_to_pass() -> None:
    value = state()
    value["specialist_results"] = {
        "research_agent": {
            "result_id": "research-1",
            "handoff_id": "handoff-r",
        },
        "code_agent": {
            "result_id": "code-1",
            "handoff_id": "handoff-c",
        },
    }
    value["specialist_verifications"] = {
        "research_agent": report(
            result_id="research-1",
            handoff_id="handoff-r",
            agent="research_agent",
        ),
        "code_agent": report(
            result_id="code-1",
            handoff_id="handoff-c",
            agent="code_agent",
        ),
    }

    decision = SupervisorAgent(StructuredModel()).decide(value)

    assert decision.action == "finish"
    assert decision.target_agent is None


def test_max_delegations_stops_only_when_more_work_is_needed() -> None:
    value = state()
    value["delegation_count"] = value["max_delegations"]

    decision = SupervisorAgent(StructuredModel()).decide(value)

    assert decision.action == "stop"

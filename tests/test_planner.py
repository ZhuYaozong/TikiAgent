"""Planner 的结构化输出和程序决策边界测试。"""

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel

from tikiagent.agents import PlannerAgent
from tikiagent.orchestration import (
    Plan,
    PlannerDecision,
    PlanningResult,
    VerificationCheck,
    VerificationReport,
)


class ScriptedStructuredModel:
    def __init__(self, responses: list[BaseModel]) -> None:
        self.responses = responses
        self.requests: list[type[BaseModel]] = []

    def complete_structured(
        self,
        messages: Sequence[Mapping[str, Any]],
        response_type: type[BaseModel],
    ) -> BaseModel:
        assert messages
        self.requests.append(response_type)
        return self.responses.pop(0)


def sample_plan() -> Plan:
    return Plan(
        goal="修复代码",
        steps=["运行测试", "修复实现", "重新测试"],
        acceptance_criteria=["测试退出码为 0"],
    )


def report(passed: bool) -> VerificationReport:
    check = VerificationCheck(
        name="tests",
        passed=passed,
        evidence=f"passed={passed}",
    )
    return VerificationReport(
        passed=passed,
        checks=[check],
        failures=[] if passed else ["tests failed"],
        evidence=[check.evidence],
        recommendation="finish" if passed else "retry",
    )


def test_initial_planning_is_forced_to_execute() -> None:
    model = ScriptedStructuredModel(
        [
            PlanningResult(
                plan=sample_plan(),
                decision=PlannerDecision(
                    action="finish",
                    instruction="",
                    reason="错误地提前结束",
                ),
            )
        ]
    )
    planner = PlannerAgent(model)

    result = planner.plan("修复代码")

    assert result.decision.action == "execute"
    assert result.decision.instruction


def test_passed_report_finishes_without_another_model_call() -> None:
    model = ScriptedStructuredModel([])
    planner = PlannerAgent(model)

    decision = planner.decide(
        task="修复代码",
        plan=sample_plan(),
        report=report(True),
        attempts=1,
        max_attempts=3,
    )

    assert decision.action == "finish"
    assert model.requests == []


def test_failed_report_at_limit_stops_without_model_call() -> None:
    model = ScriptedStructuredModel([])
    planner = PlannerAgent(model)

    decision = planner.decide(
        task="修复代码",
        plan=sample_plan(),
        report=report(False),
        attempts=2,
        max_attempts=2,
    )

    assert decision.action == "stop"
    assert model.requests == []


def test_failed_report_cannot_be_changed_to_finish() -> None:
    model = ScriptedStructuredModel(
        [
            PlannerDecision(
                action="finish",
                instruction="根据失败证据修复",
                reason="错误判断",
            )
        ]
    )
    planner = PlannerAgent(model)

    decision = planner.decide(
        task="修复代码",
        plan=sample_plan(),
        report=report(False),
        attempts=1,
        max_attempts=3,
    )

    assert decision.action == "retry"

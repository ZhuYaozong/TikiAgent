"""Plan → Execute → Verify 路由与重试策略测试。"""

from tikiagent.orchestration import (
    ActorResult,
    Plan,
    PlannerDecision,
    PlanningResult,
    PlanVerifyWorkflow,
    VerificationCheck,
    VerificationReport,
)


def sample_plan() -> Plan:
    return Plan(
        goal="修复代码",
        steps=["修复", "测试"],
        acceptance_criteria=["tests pass"],
    )


def report(passed: bool) -> VerificationReport:
    check = VerificationCheck(
        name="tests",
        passed=passed,
        evidence="exit_code=0" if passed else "exit_code=1",
    )
    return VerificationReport(
        passed=passed,
        checks=[check],
        failures=[] if passed else ["tests failed"],
        evidence=[check.evidence],
        recommendation="finish" if passed else "retry",
    )


class ScriptedPlanner:
    def __init__(self, decisions: list[PlannerDecision]) -> None:
        self.decisions = decisions
        self.decision_calls = 0

    def plan(self, task: str) -> PlanningResult:
        assert task
        return PlanningResult(
            plan=sample_plan(),
            decision=PlannerDecision(
                action="execute",
                instruction="执行首次修复",
                reason="开始执行",
            ),
        )

    def decide(self, **kwargs) -> PlannerDecision:
        assert kwargs["report"] is not None
        self.decision_calls += 1
        return self.decisions.pop(0)


class ScriptedActor:
    def __init__(self, results: list[ActorResult]) -> None:
        self.results = results
        self.instructions: list[str] = []
        self.max_steps = 8

    def execute(self, **kwargs) -> ActorResult:
        self.instructions.append(kwargs["instruction"])
        return self.results.pop(0)


class ScriptedVerifier:
    def __init__(self, reports: list[VerificationReport]) -> None:
        self.reports = reports
        self.calls = 0

    def verify(self, **kwargs) -> VerificationReport:
        assert kwargs["actor_result"] is not None
        self.calls += 1
        return self.reports.pop(0)


def actor_result(index: int) -> ActorResult:
    return ActorResult(
        completed=True,
        summary=f"actor result {index}",
        steps=index,
        tool_results=({"attempt": index},),
    )


def workflow(
    planner: ScriptedPlanner,
    actor: ScriptedActor,
    verifier: ScriptedVerifier,
    *,
    max_attempts: int = 3,
) -> PlanVerifyWorkflow:
    return PlanVerifyWorkflow(
        planner=planner,
        actor=actor,
        verifier=verifier,
        workspace_id="test-workspace",
        max_attempts=max_attempts,
    )


def test_successful_execution_finishes_after_verification() -> None:
    planner = ScriptedPlanner(
        [PlannerDecision(action="finish", instruction="", reason="pass")]
    )
    actor = ScriptedActor([actor_result(1)])
    verifier = ScriptedVerifier([report(True)])

    state = workflow(planner, actor, verifier).invoke("修复代码")

    assert state["status"] == "completed"
    assert state["attempts"] == 1
    assert state["step_count"] == 1
    assert state["verification_report"].passed is True
    assert len(state["tool_results"]) == 1


def test_failed_verification_returns_to_planner_and_retries() -> None:
    planner = ScriptedPlanner(
        [
            PlannerDecision(
                action="retry",
                instruction="根据失败证据重试",
                reason="tests failed",
            ),
            PlannerDecision(
                action="finish",
                instruction="",
                reason="tests pass",
            ),
        ]
    )
    actor = ScriptedActor([actor_result(1), actor_result(2)])
    verifier = ScriptedVerifier([report(False), report(True)])

    state = workflow(planner, actor, verifier).invoke("修复代码")

    assert state["status"] == "completed"
    assert state["attempts"] == 2
    assert state["step_count"] == 3
    assert actor.instructions == ["执行首次修复", "根据失败证据重试"]
    assert verifier.calls == 2


def test_failed_report_cannot_finish_without_retry() -> None:
    planner = ScriptedPlanner(
        [
            PlannerDecision(
                action="finish",
                instruction="修复失败项",
                reason="错误地请求结束",
            ),
            PlannerDecision(
                action="finish",
                instruction="",
                reason="通过",
            ),
        ]
    )
    actor = ScriptedActor([actor_result(1), actor_result(1)])
    verifier = ScriptedVerifier([report(False), report(True)])

    state = workflow(planner, actor, verifier).invoke("修复代码")

    assert state["status"] == "completed"
    assert state["attempts"] == 2
    assert len(actor.instructions) == 2


def test_max_attempts_stops_even_if_planner_requests_more_work() -> None:
    planner = ScriptedPlanner(
        [
            PlannerDecision(
                action="retry",
                instruction="retry 2",
                reason="failed",
            ),
            PlannerDecision(
                action="finish",
                instruction="wrong finish",
                reason="错误地结束",
            ),
        ]
    )
    actor = ScriptedActor([actor_result(1), actor_result(1)])
    verifier = ScriptedVerifier([report(False), report(False)])

    state = workflow(
        planner,
        actor,
        verifier,
        max_attempts=2,
    ).invoke("修复代码")

    assert state["status"] == "max_attempts"
    assert state["attempts"] == 2
    assert len(actor.instructions) == 2
    assert state["planner_decision"].action == "stop"


def test_graph_has_explicit_control_nodes() -> None:
    graph = workflow(
        ScriptedPlanner([]),
        ScriptedActor([]),
        ScriptedVerifier([]),
    ).graph.get_graph()

    assert {"__start__", "planner", "actor", "verifier", "__end__"} <= set(
        graph.nodes
    )

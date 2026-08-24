"""用户最终回答只能由最新且验证通过的 Specialist Result 生成。"""

from tikiagent.context.models import HistoryRecord
from tikiagent.orchestration.completion import (
    MAX_FINAL_ANSWER_CHARS,
    compose_final_answer,
)
from tikiagent.orchestration.models import (
    CodeResult,
    ResearchResult,
    ResearchSource,
    VerificationReport,
)
from tikiagent.orchestration.state import create_multi_agent_state


def _report(agent: str, *, result_id: str, handoff_id: str, passed: bool = True):
    return VerificationReport.model_validate(
        {
            "result_id": result_id,
            "handoff_id": handoff_id,
            "subject_agent": agent,
            "mode": "rules" if agent == "research_agent" else "environment",
            "passed": passed,
            "checks": [],
            "failures": [],
            "evidence": [],
            "recommendation": "finish",
        }
    )


def _state(task: str, required: list[str]):
    state = create_multi_agent_state(
        task=task,
        workspace_id="workspace-1",
        max_steps=8,
        max_delegations=4,
    )
    state["required_specialists"] = required
    return state


def test_research_answer_contains_findings_and_sources_without_internal_ids() -> None:
    state = _state("搜索新闻", ["research_agent"])
    result = ResearchResult(
        result_id="research-result-id",
        handoff_id="research-handoff-id",
        summary="Agent 框架正在加强持久化与可观测性。",
        findings=["框架 A 发布了新的持久化能力", "框架 B 改进了事件流"],
        sources=[
            ResearchSource(
                observation_id="observation-id",
                title="Framework A Release",
                url="https://example.com/release",
                snippet="官方发布说明。",
            )
        ],
    )
    state["specialist_results"] = {"research_agent": result.model_dump(mode="json")}
    state["specialist_verifications"] = {
        "research_agent": _report(
            "research_agent",
            result_id=result.result_id,
            handoff_id=result.handoff_id,
        )
    }

    answer = compose_final_answer(state)

    assert "## 调研总结" in answer
    assert "框架 A 发布了新的持久化能力" in answer
    assert "https://example.com/release" in answer
    assert "research-result-id" not in answer
    assert "research-handoff-id" not in answer


def test_hybrid_answer_combines_research_and_code_delivery() -> None:
    state = _state("调研并生成网页", ["research_agent", "code_agent"])
    research = ResearchResult(
        result_id="research-1",
        handoff_id="handoff-r",
        summary="调研完成。",
        findings=["发现一"],
        sources=[
            ResearchSource(
                observation_id="obs-1",
                title="官方来源",
                url="https://example.com/source",
                snippet="来源摘要",
            )
        ],
    )
    code = CodeResult(
        result_id="code-1",
        handoff_id="handoff-c",
        summary="对比网页已生成并验证。",
        completed=True,
        steps=3,
        changed_files=["comparison.html"],
        tests_run=["HTML structure validation"],
    )
    state["specialist_results"] = {
        "research_agent": research.model_dump(mode="json"),
        "code_agent": code.model_dump(mode="json"),
    }
    state["specialist_verifications"] = {
        "research_agent": _report(
            "research_agent", result_id="research-1", handoff_id="handoff-r"
        ),
        "code_agent": _report(
            "code_agent", result_id="code-1", handoff_id="handoff-c"
        ),
    }

    answer = compose_final_answer(state)

    assert "调研完成" in answer
    assert "comparison.html" in answer
    assert "HTML structure validation" in answer


def test_stale_verification_cannot_enter_final_answer() -> None:
    state = _state("搜索新闻", ["research_agent"])
    result = ResearchResult(
        result_id="new-result",
        handoff_id="new-handoff",
        summary="new",
    )
    state["specialist_results"] = {"research_agent": result.model_dump(mode="json")}
    state["specialist_verifications"] = {
        "research_agent": _report(
            "research_agent",
            result_id="old-result",
            handoff_id="old-handoff",
        )
    }

    try:
        compose_final_answer(state)
    except ValueError as error:
        assert "没有可用于最终回答" in str(error)
    else:  # pragma: no cover
        raise AssertionError("旧 PASS 不能进入最终回答")


def test_large_research_evidence_stays_within_final_history_limit() -> None:
    state = _state("搜索新闻", ["research_agent"])
    result = ResearchResult(
        result_id="large-result",
        handoff_id="large-handoff",
        summary="总结" * 2000,
        findings=[f"发现 {index} " + "x" * 1200 for index in range(12)],
        sources=[
            ResearchSource(
                observation_id=f"obs-{index}",
                title=f"来源 {index}",
                url=f"https://example.com/source/{index}",
                snippet="网页原始摘录" * 2000,
            )
            for index in range(10)
        ],
        unresolved_questions=["问题" * 500 for _ in range(6)],
    )
    state["specialist_results"] = {"research_agent": result.model_dump(mode="json")}
    state["specialist_verifications"] = {
        "research_agent": _report(
            "research_agent",
            result_id=result.result_id,
            handoff_id=result.handoff_id,
        )
    }

    answer = compose_final_answer(state)

    assert len(answer) <= MAX_FINAL_ANSWER_CHARS
    assert "https://example.com/source/0" in answer
    assert "网页原始摘录" not in answer
    # 与实际 Finalization 使用的 HistoryRecord 字段契约一致。
    HistoryRecord(
        record_id="final:task-1",
        task_id="task-1",
        session_id="session-1",
        record_type="result",
        producer="supervisor",
        summary=answer,
    )

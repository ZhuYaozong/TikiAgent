"""Demo 场景定义与 dry-run 测试。"""

from datetime import date

from tikiagent.demo.cli import main
from tikiagent.demo.scenarios import get_scenario


def test_three_scenarios_freeze_scope_and_expected_artifacts() -> None:
    today = date(2026, 8, 24)
    research = get_scenario("research", today=today)
    coding = get_scenario("coding", today=today)
    hybrid = get_scenario("hybrid", today=today)

    assert "仅进行 Web Research" in research.task
    assert research.expected_agents == ["research_agent"]
    assert research.expected_artifacts == []
    assert "unittest" in coding.task
    assert coding.expected_artifacts == ["calculator.py", "test_calculator.py"]
    assert hybrid.expected_agents == ["research_agent", "code_agent"]
    assert hybrid.expected_artifacts == ["comparison.html"]
    assert "内部 messages 不得直接传" in hybrid.task


def test_cli_dry_run_only_prints_scenario(capsys) -> None:
    exit_code = main(["hybrid", "--dry-run", "--json"])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert '"name": "hybrid"' in output
    assert '"expected_artifacts"' in output

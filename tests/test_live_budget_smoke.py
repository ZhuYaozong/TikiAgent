"""真实测试入口默认离线，以及整批额度/截止时间的回归。"""
import importlib.util
import json
from types import SimpleNamespace
from pathlib import Path

import pytest

from tikiagent.harness.persistence.budget import RequestBudgetExceeded


@pytest.fixture
def smoke():
    spec = importlib.util.spec_from_file_location("live_budget_smoke", Path(__file__).parents[1] / "examples/live_budget_smoke.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_default_entry_does_not_call_api(smoke, monkeypatch):
    monkeypatch.setattr("sys.argv", ["live_budget_smoke"])
    monkeypatch.setattr(smoke, "run", lambda args: pytest.fail("默认入口不得调用真实 API"))
    assert smoke.main() == 0


def test_global_budget_is_shared_across_tasks_and_reserves_final(smoke, tmp_path):
    budget = smoke.BatchBudget(tmp_path, model_limit=18, web_limit=2)
    for task in ["a", "b"]:
        budget.bind("s", task)
        budget.model_request()
    with pytest.raises(RequestBudgetExceeded):
        budget.model_request()
    budget.model_request(final=True)
    state = json.loads(next((tmp_path / "batch").glob("*.json")).read_text())
    assert state["model"] == 3 and state["work"] == 2


def test_deadline_blocks_work_and_web_but_not_final(smoke, tmp_path):
    now = [1]
    budget = smoke.BatchBudget(tmp_path, model_limit=20, web_limit=2, clock=lambda: now[0])
    budget.bind("s", "a")
    budget.start_scenario(2)
    now[0] = 3
    with pytest.raises(RequestBudgetExceeded):
        budget.model_request()
    with pytest.raises(RequestBudgetExceeded):
        budget.web_request("query")
    budget.model_request(final=True)


def test_web_budget_is_shared_and_duplicate_query_is_not_sent(smoke, tmp_path):
    budget = smoke.BatchBudget(tmp_path, model_limit=20, web_limit=2)
    budget.bind("s", "a")
    budget.web_request("query")
    with pytest.raises(RequestBudgetExceeded):
        budget.web_request("query")
    budget.bind("s", "b")
    budget.web_request("query")
    with pytest.raises(RequestBudgetExceeded):
        budget.web_request("other query")


def test_metrics_do_not_count_agent_forwarding_twice(smoke):
    events = [SimpleNamespace(event_type="model_response", data={"stage": stage, "max_output_tokens": 16384})
        for stage in ["supervisor", None, "verifier", "verification", "verifier_final", "verification_finalization"]]
    data = smoke.aggregate_metrics(events)
    assert [r["stage"] for r in data["model_responses"]] == ["supervisor", "verifier", "verifier_final"]

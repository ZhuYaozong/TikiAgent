"""有界真实 API 冒烟：独立 Session/Workspace，不自动重跑失败任务。"""
from __future__ import annotations

import argparse
from datetime import date
from html.parser import HTMLParser
import json
import math
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4
from types import SimpleNamespace

from tikiagent.application.bootstrap import ApplicationRuntimeFactory
from tikiagent.application.events import CollectingEventSink, EventBus
from tikiagent.context.memory.history import JsonlHistoryStore
from tikiagent.demo.scenarios import get_scenario
from tikiagent.harness.persistence.budget import RequestBudget, RequestBudgetExceeded
from tikiagent.providers.llm.config import ModelSettings
from tikiagent.harness.persistence.trace import JsonlTraceStore
from tikiagent.runtime.policy import OUTPUT_LIMITS


class BatchBudget(RequestBudget):
    """每次实际请求同时扣任务和整批额度；截止后仍允许有界收尾。"""

    def __init__(self, root: Path, *, model_limit: int, web_limit: int, clock=time.monotonic):
        super().__init__(root / "tasks", model_limit=model_limit, web_limit=web_limit)
        self.batch = RequestBudget(root / "batch", model_limit=model_limit, web_limit=web_limit)
        self.batch.bind("live-smoke", "batch")
        self.clock = clock
        self.deadline = float("inf")

    def start_scenario(self, seconds: float):
        self.deadline = self.clock() + seconds

    def model_request(self, *, final=False):
        if not final and self.clock() >= self.deadline:
            raise RequestBudgetExceeded("真实测试场景超时，禁止新工作请求；仅允许收尾")
        # 多扣但未发送也计尝试，保守停机，不把请求失败当作退款。
        super().model_request(final=final)
        self.batch.model_request(final=final)

    def web_request(self, key):
        if self.clock() >= self.deadline:
            raise RequestBudgetExceeded("真实测试场景超时，禁止新联网请求")
        super().web_request(key)
        self.batch.web_request(f"{self.scope.get()}:{key}")


class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = set()
        self.text = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.links.update(value for key, value in attrs if key == "href" and value)

    def handle_data(self, data):
        self.text.append(data)


def independent_checks(name, data_dir, session_id, task_id):
    """独立检查交付；不把 Supervisor 的完成宣告当成实际验收。"""
    workspace = data_dir / "workspaces" / session_id
    history = JsonlHistoryStore(data_dir / "histories" / f"{session_id}.jsonl")
    records = history.list_records(task_id=task_id, session_id=session_id)
    results = [r for r in records if r.record_type == "result"]
    research = [r.payload for r in results if r.producer == "research_agent"]
    sources = {s["url"] for result in research for s in result.get("sources", [])}
    agents = {r.producer for r in results}
    checks = {}
    if name in {"research", "hybrid"}:
        checks["research_result"] = "research_agent" in agents
        checks["source_count"] = len(sources) >= (3 if name == "research" else 2)
        checks["findings"] = any(result.get("findings") for result in research)
        observed = {url for result in research for observation in result.get("observations", []) for url in observation.get("urls", [])}
        checks["sources_from_observations"] = bool(sources) and sources <= observed
    if name == "research":
        checks["no_artifacts"] = not any(workspace.rglob("*")) if workspace.exists() else True
    if name == "coding":
        checks["files"] = all((workspace / p).is_file() for p in ["calculator.py", "test_calculator.py"])
        checks["code_result"] = "code_agent" in agents
        if checks["files"]:
            # 只运行隔离 Workspace 中的标准库测试；记录退出码，不输出原始正文。
            completed = subprocess.run([sys.executable, "-m", "unittest", "discover", "-v"],
                cwd=workspace, capture_output=True, text=True, timeout=30)
            checks["unittest_exit_zero"] = completed.returncode == 0
            checks["tests_discovered"] = "Ran 0 tests" not in completed.stderr and "Ran " in completed.stderr
    if name == "hybrid":
        checks["code_result"] = "code_agent" in agents
        path = workspace / "comparison.html"
        checks["html_exists"] = path.is_file()
        if path.is_file():
            parser = LinkParser()
            parser.feed(path.read_text(encoding="utf-8"))
            checks["source_links"] = len(parser.links & sources) >= 2
            checks["visible_content"] = len("".join(parser.text).strip()) >= 100
    return checks, sorted(sources), sorted(agents)


def aggregate_metrics(events):
    calls, contexts = [], []
    for event in events:
        if (event.event_type == "model_response" and "max_output_tokens" in event.data
                and event.data.get("stage") in OUTPUT_LIMITS):
            calls.append({key: event.data.get(key) for key in
                ["stage", "max_output_tokens", "reasoning_effort", "finish_reason", "usage"]})
        elif event.event_type == "context_prepared":
            contexts.append({key: event.data.get(key) for key in ["agent", "before", "after", "actions"]})
    return {"model_responses": calls, "contexts": contexts,
        "tool_requests": sum(e.event_type == "tool_call_requested" for e in events)}


def summarize_saved_report(root: Path):
    """仅从测试审计记录重建计量报告，不恢复执行、不开 API。"""
    path = root / "report.json"
    report = json.loads(path.read_text(encoding="utf-8"))
    for item in report["scenarios"]:
        events = JsonlTraceStore(root / "traces" / f"{item['session_id']}.jsonl").list_events()
        item.update(aggregate_metrics([SimpleNamespace(event_type=e.event_type, data=e.details) for e in events]))
    batch = json.loads(next((root / "smoke-budget" / "batch").glob("*.json")).read_text(encoding="utf-8"))
    report["attempt_totals"] = {key: batch[key] for key in ["model", "work", "web", "model_limit", "web_limit"]}
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Rebuilt metrics only (no API): {path}")


def run(args):
    root = Path(args.data_dir).resolve() / f"budget-smoke-{uuid4().hex}"
    bus = EventBus()
    sink = CollectingEventSink()
    bus.subscribe(sink)
    factory = ApplicationRuntimeFactory(root, env_file=args.env_file, event_bus=bus)
    ledger = BatchBudget(root / "smoke-budget", model_limit=min(args.model_limit, factory.policy.model_requests),
        web_limit=min(args.web_limit, factory.policy.task_web_tools))
    factory.request_budget = ledger
    controller = factory.build_controller()
    settings = ModelSettings.from_env(args.env_file)
    report = {"data_dir": str(root), "model": settings.model, "api_style": settings.api_style,
        "context_limit": factory.context_limit,
        "policy": factory.policy.model_dump(), "context": factory.context_budget.model_dump(), "scenarios": []}
    print(f"Isolated data: {root}", flush=True)

    class Progress:
        def handle(self, event):
            if event.event_type in {"tool_execution_started", "model_response", "verification_completed", "workflow_completed", "workflow_failed"}:
                stage = event.data.get("stage", event.data.get("tool_name", ""))
                print(f"  {event.sequence}: {event.event_type} {stage}", flush=True)
    bus.subscribe(Progress())
    for name in args.scenarios:
        session = controller.new_session(workspace_id=f"smoke-{name}")
        start_index = len(sink.events)
        started = time.monotonic()
        ledger.start_scenario(args.scenario_seconds)
        ledger.bind(session.session_id, "pre-router")
        task = get_scenario(name).task
        if name == "research":
            task = (f"今天是 {date.today().isoformat()}。只联网研究，不写文件。搜索最近30天的3条重要 AI Agent 新闻；"
                "按日期、变化、意义总结，至少3个不同来源URL，优先官方公告；不确定日期需明确说明。证据足够就总结，不重复搜索。")
        item = {"scenario": name, "session_id": session.session_id}
        print(f"Scenario: {name}", flush=True)
        try:
            outcome = controller.submit(session_id=session.session_id, user_input=task)
            item.update(status=outcome.status, task_id=outcome.task_id, checkpoint_id=outcome.checkpoint_id)
            # 保留最终可见答案供人工核对新闻日期与出处，不复制私有推理。
            (root / f"{name}-answer.md").write_text(outcome.message, encoding="utf-8")
            checks, sources, agents = independent_checks(name, root, session.session_id, outcome.task_id)
            item.update(checks=checks, sources=sources, agents=agents,
                passed=outcome.status == "workflow_completed" and all(checks.values()))
        except Exception as error:
            # 供应商异常正文可能含敏感信息，只保存类别；不自动重跑任务。
            item.update(status="error", error_type=type(error).__name__, passed=False)
        item.update(seconds=round(time.monotonic() - started, 2), **aggregate_metrics(sink.events[start_index:]))
        report["scenarios"].append(item)
        # 整批账本包含 Router 和网络失败尝试，不等同于已返回响应的数量。
        batch_files = list((root / "smoke-budget" / "batch").glob("*.json"))
        if batch_files:
            consumed = json.loads(batch_files[0].read_text(encoding="utf-8"))
            report["attempt_totals"] = {key: consumed[key] for key in ["model", "work", "web", "model_limit", "web_limit"]}
        print(f"Result: {name} {item['status']} passed={item['passed']} ({item['seconds']}s)", flush=True)
        # 每个场景后保存报告，进程中断仍能找到已经结束的结果。
        (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Report: {root / 'report.json'}", flush=True)
    return 0 if all(item["passed"] for item in report["scenarios"]) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-live", action="store_true", help="确认调用真实付费 API")
    parser.add_argument("--summarize-data", type=Path, help="只重新汇总既有测试目录的安全计量，不调用 API")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--data-dir", default=".tiki-demo")
    parser.add_argument("--scenarios", nargs="+", choices=["research", "coding", "hybrid"], default=["research", "coding", "hybrid"])
    parser.add_argument("--model-limit", type=int, default=128)
    parser.add_argument("--web-limit", type=int, default=20)
    parser.add_argument("--scenario-seconds", type=float, default=480)
    args = parser.parse_args()
    if args.summarize_data:
        summarize_saved_report(args.summarize_data.resolve())
        return 0
    if not args.run_live:
        print("未调用 API。添加 --run-live 才执行；默认整批最多128次模型/20次联网，每个任务仅一次。")
        return 0
    if args.model_limit <= 16 or args.web_limit < 1 or not math.isfinite(args.scenario_seconds) or args.scenario_seconds <= 0:
        parser.error("模型上限需>16，联网上限和场景时间需>0")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())

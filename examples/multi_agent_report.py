"""真实 Supervisor Graph：调研后生成并验证来源可追溯的网页。"""

import argparse
import json
import sys
from pathlib import Path

from tikiagent.agents import (
    CodeEnvironmentVerifier,
    CommandCheck,
    MultiAgentCodeAgent,
    ResumableReActAgent,
    ResearchAgent,
    ResearchResultVerifier,
    SupervisorAgent,
)
from tikiagent.harness import (
    Dispatcher,
    ExecutionCoordinator,
    ExecutionHarness,
    FixedCommandPermissionPolicy,
    JsonCheckpointStore,
    JsonlTraceStore,
    SearchSettings,
    TavilyProvider,
    Workspace,
    build_file_registry,
    build_read_only_file_registry,
    build_web_registry,
    register_command_tool,
)
from tikiagent.context import BaseContext, JsonlHistoryStore
from tikiagent.harness import ExecutionContext
from tikiagent.llm import ModelSettings, OpenAICompatibleClient
from tikiagent.orchestration import (
    Handoff,
    MultiAgentWorkflow,
    ResearchResult,
    TikiState,
    VerificationGate,
)


CODE_SYSTEM_PROMPT = """你是 TikiAgent CodeAgent。
只执行 Supervisor 当前 Handoff，不负责宣布整个任务完成。
必须通过工具观察并修改 Workspace，目标文件固定为 comparison.html。
如果 Base Context 包含 ResearchResult，使用其中事实并加入真实来源链接。
使用 HTML/CSS 和 Python 标准库，不安装依赖，不访问 Workspace 外文件。
完成后重新读取文件或运行检查。最终是否通过由 Verification Gate 决定。
"""


HTML_CHECK = """
from html.parser import HTMLParser
from pathlib import Path

class Checker(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = set()

    def handle_starttag(self, tag, attrs):
        self.tags.add(tag)

content = Path('comparison.html').read_text(encoding='utf-8')
checker = Checker()
checker.feed(content)
required = {'html', 'head', 'title', 'body'}
missing = sorted(required - checker.tags)
if missing:
    raise SystemExit('missing tags: ' + ','.join(missing))
print('html structure ok')
""".strip()


class LazyResearchAgent:
    """只有 Supervisor 真正路由到 ResearchAgent 时才读取 Tavily 配置。"""

    def __init__(self, model: OpenAICompatibleClient) -> None:
        self.model = model
        self.agent: ResearchAgent | None = None
        self.supports_harness = True

    def run(
        self,
        handoff: Handoff,
        base_context: BaseContext,
        execution_context: ExecutionContext | None = None,
    ) -> ResearchResult:
        if self.agent is None:
            registry = build_web_registry(
                TavilyProvider(SearchSettings.from_env())
            )
            dispatcher = Dispatcher(registry)
            self.agent = ResearchAgent(
                model=self.model,
                structured_model=self.model,
                dispatcher=dispatcher,
                execution_harness=ExecutionHarness(dispatcher),
            )
        return self.agent.run(
            handoff,
            base_context,
            execution_context=execution_context,
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--task",
        default=(
            "调研最近 Agent Framework 的重要变化，并根据调研结果生成"
            "带真实来源链接的 comparison.html 对比网页。"
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    workspace = Workspace(Path(".tiki") / "multi-agent-workspace")
    model = OpenAICompatibleClient(ModelSettings.from_env())

    code_registry = build_file_registry(workspace)
    register_command_tool(code_registry, workspace)
    code_dispatcher = Dispatcher(code_registry)
    checkpoint_store = JsonCheckpointStore(Path(".tiki") / "checkpoints")
    trace_store = JsonlTraceStore(Path(".tiki") / "events.jsonl")
    code_agent = MultiAgentCodeAgent(
        ResumableReActAgent(
            model=model,
            dispatcher=code_dispatcher,
            system_prompt=CODE_SYSTEM_PROMPT,
            max_steps=12,
            execution_coordinator=ExecutionCoordinator(
                ExecutionHarness(code_dispatcher),
                checkpoint_store,
                trace_store,
            ),
        )
    )

    # Gate 的 Code 策略只能读取文件；命令内容由应用固定，不由模型生成。
    verifier_registry = build_read_only_file_registry(workspace)
    register_command_tool(verifier_registry, workspace)
    verifier_dispatcher = Dispatcher(verifier_registry)
    gate = VerificationGate(
        research_verifier=ResearchResultVerifier(min_sources=1),
        code_verifier=CodeEnvironmentVerifier(
            dispatcher=verifier_dispatcher,
            execution_harness=ExecutionHarness(
                verifier_dispatcher,
                permission_policy=FixedCommandPermissionPolicy(
                    allowed_commands={
                        (sys.executable, "-B", "-c", HTML_CHECK)
                    }
                ),
            ),
            checks=(
                CommandCheck(
                    name="html-structure",
                    command=(sys.executable, "-B", "-c", HTML_CHECK),
                ),
            ),
            expected_files=("comparison.html",),
        ),
    )
    workflow = MultiAgentWorkflow(
        supervisor=SupervisorAgent(model),
        research_agent=LazyResearchAgent(model),
        code_agent=code_agent,
        verification_gate=gate,
        workspace_id="multi-agent-demo",
        max_delegations=5,
        history_store=JsonlHistoryStore(Path(".tiki") / "history.jsonl"),
    )

    final_state: TikiState | None = None
    seen_events = 0
    for snapshot in workflow.stream(args.task):
        final_state = snapshot
        for event in snapshot["recent_events"][seen_events:]:
            print(f"[Event] {event}")
        seen_events = len(snapshot["recent_events"])
    if final_state is None:
        raise RuntimeError("MultiAgentWorkflow 没有产生最终状态")

    print("\n[Final]")
    print(
        f"status={final_state['status']} "
        f"delegations={final_state['delegation_count']}"
    )
    print(final_state["final_result"])
    print("[Task Board]")
    print(
        json.dumps(
            [
                item.model_dump(mode="json")
                for item in final_state["task_board"].items.values()
            ],
            ensure_ascii=False,
            indent=2,
        )
    )
    print("[History]")
    print(
        json.dumps(
            [
                {
                    "sequence": item.sequence,
                    "record_id": item.record_id,
                    "record_type": item.record_type,
                    "producer": item.producer,
                    "refs": item.refs,
                }
                for item in workflow.history_for(final_state)
            ],
            ensure_ascii=False,
            indent=2,
        )
    )
    print(
        "artifact="
        f"{workspace.resolve('comparison.html') if 'code_agent' in final_state['specialist_results'] else None}"
    )


if __name__ == "__main__":
    main()

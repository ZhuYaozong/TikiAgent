"""使用 LangGraph、TikiState 和真实模型运行代码修复闭环。"""

from pathlib import Path

from tikiagent.harness import (
    Dispatcher,
    Workspace,
    build_file_registry,
    register_command_tool,
)
from tikiagent.llm import ModelSettings, OpenAICompatibleClient
from tikiagent.orchestration import ReActWorkflow, TikiState


CODE_AGENT_PROMPT = """你是 TikiAgent 的代码修复 Agent。
只能通过工具观察、修改和验证 Workspace，不要假设环境状态。
不允许修改或删除测试来让测试通过。
修改前运行现有测试取得失败基线，修复后再次运行测试。
Python 测试使用：
['python', '-B', '-m', 'unittest', 'discover', '-s', '.', '-p', 'test_*.py', '-v']。
只有 timed_out=false 且 exit_code=0，才能声称测试通过。
"""


def prepare_workspace(workspace: Workspace) -> None:
    workspace.resolve("calculator.py").write_text(
        "def add(a: int, b: int) -> int:\n"
        "    return a - b\n",
        encoding="utf-8",
    )
    workspace.resolve("test_calculator.py").write_text(
        "import unittest\n\n"
        "from calculator import add\n\n\n"
        "class TestAdd(unittest.TestCase):\n"
        "    def test_positive_numbers(self) -> None:\n"
        "        self.assertEqual(add(2, 3), 5)\n\n"
        "    def test_negative_numbers(self) -> None:\n"
        "        self.assertEqual(add(-2, 1), -1)\n",
        encoding="utf-8",
    )


def main() -> None:
    workspace = Workspace(Path(".tiki") / "langgraph-workspace")
    prepare_workspace(workspace)

    model = OpenAICompatibleClient(ModelSettings.from_env())
    registry = build_file_registry(workspace)
    register_command_tool(registry, workspace)
    workflow = ReActWorkflow(
        model=model,
        dispatcher=Dispatcher(registry),
        workspace_id="langgraph-demo",
        system_prompt=CODE_AGENT_PROMPT,
        max_steps=12,
    )

    task = (
        "修复 calculator.py 中的实现错误，使现有自动化测试全部通过。"
        "必须提供实际测试结果作为证据。"
    )
    final_state: TikiState | None = None

    for snapshot in workflow.stream(task):
        final_state = snapshot
        print(
            "[State] "
            f"status={snapshot['status']} "
            f"step={snapshot['step_count']} "
            f"pending={len(snapshot['pending_tool_calls'])} "
            f"results={len(snapshot['tool_results'])}"
        )

    if final_state is None:
        raise RuntimeError("LangGraph 没有产生最终状态")

    print("\n[Final Result]")
    print(final_state["final_result"])


if __name__ == "__main__":
    main()

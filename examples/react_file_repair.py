"""使用真实 OpenAI-compatible 模型修复 Workspace 文件。"""

from pathlib import Path

from tikiagent.agents import ReActAgent
from tikiagent.harness import (
    Dispatcher,
    Workspace,
    build_file_registry,
    register_command_tool,
)
from tikiagent.llm import ModelSettings, OpenAICompatibleClient


CODE_AGENT_PROMPT = """你是 TikiAgent 的代码修复 Agent。
只能通过工具观察、修改和验证 Workspace，不要假设环境状态。
必须在修改前运行现有自动化测试取得失败基线，且不允许修改测试。
修复后必须再次运行测试。只有 timed_out=false 且 exit_code=0，才能声称测试通过。
使用 Python 内置 unittest，并用 -B 避免快速改写后读取旧字节码。
推荐命令为 ['python', '-B', '-m', 'unittest', 'discover', '-s', '.', '-p', 'test_*.py', '-v']。
工具失败时分析结构化结果并调整下一步。
"""


def main() -> None:
    workspace = Workspace(Path(".tiki") / "workspace")
    calculator = workspace.resolve("calculator.py")
    calculator.write_text(
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

    settings = ModelSettings.from_env()
    model = OpenAICompatibleClient(settings)
    registry = build_file_registry(workspace)
    register_command_tool(registry, workspace)
    dispatcher = Dispatcher(registry)
    agent = ReActAgent(
        model=model,
        dispatcher=dispatcher,
        system_prompt=CODE_AGENT_PROMPT,
        max_steps=12,
    )

    result = agent.run(
        "修复 calculator.py 中的实现错误，使现有自动化测试全部通过。"
        "必须提供实际测试结果作为完成证据。"
    )

    for tool_result in result.tool_results:
        print(
            f"[{tool_result.tool_name}] "
            f"ok={tool_result.ok} "
            f"output={tool_result.output} "
            f"error={tool_result.error}"
        )
    print(f"\n{result.final_text}")


if __name__ == "__main__":
    main()

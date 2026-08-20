"""使用真实 OpenAI-compatible 模型修复 Workspace 文件。"""

from pathlib import Path

from tikiagent.agents import ReActAgent
from tikiagent.harness import Dispatcher, Workspace, build_file_registry
from tikiagent.llm import ModelSettings, OpenAICompatibleClient


def main() -> None:
    workspace = Workspace(Path(".tiki") / "workspace")
    calculator = workspace.resolve("calculator.py")
    calculator.write_text(
        "def add(a: int, b: int) -> int:\n"
        "    return a - b\n",
        encoding="utf-8",
    )

    settings = ModelSettings.from_env()
    model = OpenAICompatibleClient(settings)
    dispatcher = Dispatcher(build_file_registry(workspace))
    agent = ReActAgent(model=model, dispatcher=dispatcher)

    result = agent.run(
        "检查 calculator.py 的 add 函数；如果实现错误就修复，"
        "并在最终回答前重新读取文件确认结果。"
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

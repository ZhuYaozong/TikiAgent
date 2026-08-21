"""使用真实模型运行 Plan → Execute → Verify 代码修复闭环。"""

import sys
from pathlib import Path

from tikiagent.agents import (
    CommandCheck,
    EnvironmentVerifier,
    PlannerAgent,
    ReActAgent,
    ReActCodeActor,
)
from tikiagent.harness import (
    Dispatcher,
    Workspace,
    build_file_registry,
    build_read_only_file_registry,
    register_command_tool,
)
from tikiagent.llm import ModelSettings, OpenAICompatibleClient
from tikiagent.orchestration import PlanVerifyWorkflow, TikiState


CODE_ACTOR_PROMPT = """你是 TikiAgent 的 Code Actor。
只负责执行 Planner 的本轮指令，不负责宣布整个工作流完成。
只能通过工具观察、修改和验证 Workspace，不要假设环境状态。
不允许修改或删除测试来让测试通过。
修改前取得失败基线，修改后重新运行测试。
只有 timed_out=false 且 exit_code=0 才能声称命令成功。
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
    workspace = Workspace(Path(".tiki") / "plan-verify-workspace")
    prepare_workspace(workspace)

    model = OpenAICompatibleClient(ModelSettings.from_env())

    actor_registry = build_file_registry(workspace)
    register_command_tool(actor_registry, workspace)
    actor = ReActCodeActor(
        ReActAgent(
            model=model,
            dispatcher=Dispatcher(actor_registry),
            system_prompt=CODE_ACTOR_PROMPT,
            max_steps=12,
        )
    )

    # Verifier 不获得 write_file / edit_file，命令也由应用固定配置。
    verifier_registry = build_read_only_file_registry(workspace)
    register_command_tool(verifier_registry, workspace)
    verifier = EnvironmentVerifier(
        Dispatcher(verifier_registry),
        (
            CommandCheck(
                name="python-unittest",
                command=(
                    sys.executable,
                    "-B",
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    ".",
                    "-p",
                    "test_*.py",
                    "-v",
                ),
            ),
        ),
    )
    workflow = PlanVerifyWorkflow(
        planner=PlannerAgent(model),
        actor=actor,
        verifier=verifier,
        workspace_id="plan-verify-demo",
        max_attempts=3,
    )

    task = (
        "修复 calculator.py 中的实现错误，使现有自动化测试全部通过，"
        "并提供真实测试结果作为证据。"
    )
    final_state: TikiState | None = None
    previous_signature: tuple[object, ...] | None = None

    for snapshot in workflow.stream(task):
        final_state = snapshot
        decision = snapshot["planner_decision"]
        report = snapshot["verification_report"]
        signature = (
            snapshot["status"],
            snapshot["attempts"],
            decision.action if decision else None,
            report.passed if report else None,
        )
        if signature == previous_signature:
            continue
        previous_signature = signature
        print(
            "[Workflow] "
            f"status={signature[0]} "
            f"attempts={signature[1]} "
            f"decision={signature[2]} "
            f"verified={signature[3]}"
        )

    if final_state is None:
        raise RuntimeError("PlanVerifyWorkflow 没有产生最终状态")

    print("\n[Final Result]")
    print(final_state["final_result"])


if __name__ == "__main__":
    main()

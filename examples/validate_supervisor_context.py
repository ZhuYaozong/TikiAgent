"""显式 --live 才调用真实 API；在临时 Workspace 验证规划 Agent 与两层摘要。"""

import argparse
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from tikiagent.application.bootstrap import ApplicationRuntimeFactory
from tikiagent.context.compression.llm import SummaryEngine, LLMBaseCompressor, LLMLocalCompressor
from tikiagent.context.memory.models import HistoryRecord, LocalMemory, ReActInteraction
from tikiagent.context.models import BaseContext, WorkingMemory
from tikiagent.providers.llm.config import ModelSettings
from tikiagent.providers.llm.openai_compatible import OpenAICompatibleClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if not args.live:
        print("需显式传入 --live；将调用配置的模型并产生 API 费用。")
        return
    settings = replace(ModelSettings.from_env(args.env_file), timeout_seconds=45, max_retries=0)
    model = OpenAICompatibleClient(settings)
    # 全部实验数据均在临时目录，绝不读取现有 Session 或修改用户工作区。
    with TemporaryDirectory(prefix="tiki-supervisor-smoke-") as directory:
        factory = ApplicationRuntimeFactory(Path(directory), env_file=args.env_file)
        factory.model = model
        workflow = factory._build_workflow("smoke-session", "smoke-workspace")
        workflow.research_agent = None
        workflow.supervisor.max_steps = 8
        workflow.max_delegations = 2
        workflow.code_agent.agent.max_steps = 4
        workspace = Path(directory) / "workspaces" / "smoke-session"
        (workspace / "input.txt").write_text("项目名称 TikiAgent。当前测试值为 42。", encoding="utf-8")
        result = workflow.invoke("这是只读 inspection 任务：读取当前 Workspace 的 input.txt，告诉我项目名称和测试值。只需一个 Todo，不修改文件、不运行命令。",
                                 session_id="smoke-session", task_id="smoke-task")
        if result["status"] != "completed":
            raise RuntimeError("Supervisor 冒烟未完成：" + result["status"])
        print(json.dumps({"workflow": result["status"], "delegations": result["delegation_count"],
                          "supervisor_steps": result["supervisor_runtime"]["steps"]}, ensure_ascii=False), flush=True)

    engine = SummaryEngine(model)
    records = [HistoryRecord(record_id=f"record-{i}", task_id="t", session_id="s", record_type="result", producer="code_agent",
                              summary="已修改 calculator.py；测试仍失败，需要继续修复。" + "重复的调试日志。" * 150)
               for i in range(3)]
    context = BaseContext(agent="supervisor", working_memory=WorkingMemory(task="修复计算器", instruction="继续规划", phase="orchestration", relevant_history=records))
    history = LLMBaseCompressor(engine).compress(context)
    if not history.changed:
        raise RuntimeError("真实 History 摘要未通过校验")
    interactions = []
    for i in range(3):
        interactions.append(ReActInteraction(interaction_id=f"turn-{i}",
            assistant_message={"role": "assistant", "tool_calls": [{"id": f"call-{i}", "type": "function", "function": {"name": "run_command", "arguments": '{"command":["python","-m","unittest"]}'}}]},
            tool_messages=[{"role": "tool", "tool_call_id": f"call-{i}", "content": json.dumps({"ok": True, "output": {"exit_code": 1, "stderr": "AssertionError: 2 != 3\n" * 100}})}]))
    local = LLMLocalCompressor(engine).compress(LocalMemory(recent_interactions=interactions), recent_interaction_limit=1, task="修复计算器")
    if not local.changed or local.memory.execution_facts[0]["exit_code"] != 1:
        raise RuntimeError("真实 Local 摘要未通过校验")
    print(json.dumps({"history_summary": True, "local_summary": True, "summary_calls": engine.calls,
                      "history_source_count": len(history.context.working_memory.history_summary_refs),
                      "recent_interactions": len(local.memory.recent_interactions)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # 不把供应商正文、Authorization 或模型生成内容写进终端诊断。
        print(json.dumps({"status": "failed", "error_type": type(error).__name__}), flush=True)
        raise SystemExit(1) from None

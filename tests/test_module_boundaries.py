"""模块重构的启动隔离与持久化兼容契约。"""

from pathlib import Path
import hashlib
import json
import subprocess
import sys

from pydantic import TypeAdapter

from tikiagent.context.memory.models import HistoryRecord
from tikiagent.harness.permissions.models import ApprovalRequest
from tikiagent.harness.persistence.checkpoint import ExecutionCheckpoint
from tikiagent.orchestration.state import TikiState
from tikiagent.tools.models import ToolResult


ROOT = Path(__file__).resolve().parents[1]


def test_production_bootstrap_does_not_import_baselines() -> None:
    # 使用新进程，避免其他测试已经导入 Baseline 干扰启动依赖检查。
    code = (
        "import sys; "
        "from tikiagent.application.bootstrap import ApplicationRuntimeFactory; "
        "from tikiagent.interfaces.tui.app import TikiTuiApp; "
        "assert not any(n.startswith('tikiagent.baselines') for n in sys.modules)"
    )
    subprocess.run([sys.executable, "-c", code], cwd=ROOT, check=True)


def test_public_package_exports_remain_available() -> None:
    # 旧包级 API 保留相同对象身份，实现只存在于新模块。
    from tikiagent.agents import ReActAgent, PlannerAgent
    from tikiagent.baselines.planner import PlannerAgent as BaselinePlanner
    from tikiagent.harness import ToolResult as PublicToolResult
    from tikiagent.runtime.react import ReActAgent as RuntimeReAct

    assert ReActAgent is RuntimeReAct
    assert PlannerAgent is BaselinePlanner
    assert PublicToolResult is ToolResult


def test_unchanged_protocols_match_v1_and_new_execution_fields_are_optional() -> None:
    # 摘要来自重构前提交 7ecd965，约束路径调整不改变已有持久化协议。
    # 后续有意升级协议时，应同时提供迁移策略再更新此快照。
    expected = {
        "approval": "34851dd81eeda4df526099bf60576c0ba1c54d7ab72f69177e60a2da72a23818",
        "tool_result": "7f49049b68c1d1ee3d8eb5f5f501eaf1ea64ea9cd08c879ea23a8b645d4ce9e2",
        "history": "479732d958f0080ba7d6280977c744b071d4cca655501cc4e2804c809b25adc9",
    }
    models = {
        "approval": ApprovalRequest,
        "tool_result": ToolResult,
        "history": HistoryRecord,
    }
    for name, model in models.items():
        schema = json.dumps(TypeAdapter(model).json_schema(), sort_keys=True)
        assert hashlib.sha256(schema.encode()).hexdigest() == expected[name]
    # State/Checkpoint 已增加带默认值的模式和预算；旧快照迁移另有恢复测试。
    assert TypeAdapter(TikiState).json_schema()["properties"]["max_code_tool_calls"]["type"] == "integer"
    snapshot_schema = ExecutionCheckpoint.model_json_schema()["$defs"]["ReActRunSnapshot"]
    assert snapshot_schema["properties"]["loop_guard"]["type"] == "object"
    assert "loop_guard" not in snapshot_schema["required"]
    assert "max_code_tool_calls" not in TypeAdapter(TikiState).json_schema()["required"]

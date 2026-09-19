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


def test_persisted_schemas_match_v1_baseline() -> None:
    # 摘要来自重构前提交 7ecd965，约束路径调整不改变已有持久化协议。
    # 后续有意升级协议时，应同时提供迁移策略再更新此快照。
    expected = {
        "checkpoint": "ab3644d6a70de5f48fef1a1967bb46ea23616f82848e50e4c2cb261d6a665ba7",
        "state": "f516030f2e53203c8ef543519d64166d78fc4a78c545067c8c0ebc26a0e55e12",
        "approval": "34851dd81eeda4df526099bf60576c0ba1c54d7ab72f69177e60a2da72a23818",
        "tool_result": "7f49049b68c1d1ee3d8eb5f5f501eaf1ea64ea9cd08c879ea23a8b645d4ce9e2",
        "history": "479732d958f0080ba7d6280977c744b071d4cca655501cc4e2804c809b25adc9",
    }
    models = {
        "checkpoint": ExecutionCheckpoint,
        "state": TikiState,
        "approval": ApprovalRequest,
        "tool_result": ToolResult,
        "history": HistoryRecord,
    }
    for name, model in models.items():
        schema = json.dumps(TypeAdapter(model).json_schema(), sort_keys=True)
        assert hashlib.sha256(schema.encode()).hexdigest() == expected[name]

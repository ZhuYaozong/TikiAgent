"""固定解释器的环境取证入口；不接受模型提供的 Python 代码或任意 argv。"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from tikiagent.harness.workspace import Workspace
from tikiagent.tools.models import ToolExecutionError
from tikiagent.tools.registry import RegisteredTool, ToolRegistry


class PackageArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    package: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")


class ImportArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    module: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$", max_length=150)


class TestArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    runner: Literal["unittest", "pytest"] = "unittest"


def _run(script: str, argument: str, cwd: Path, *, timeout: int = 20) -> dict:
    # 子进程不继承 API 凭据；隔离模式不加载 Workspace 的同名标准库模块。
    env = {k: v for k, v in os.environ.items() if k.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP"}}
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    try:
        result = subprocess.run([sys.executable, "-I", "-B", "-c", script, argument],
                                cwd=cwd, env=env, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=timeout, shell=False)
    except subprocess.TimeoutExpired:
        return {"interpreter": sys.executable, "exit_code": None, "timed_out": True, "verified": False}
    # 结果有限长；verified 来自固定脚本，不从被执行包的 stdout 猜成功。
    output = {"interpreter": sys.executable, "exit_code": result.returncode, "timed_out": False,
              "stdout": result.stdout[-4000:], "stderr": result.stderr[-4000:]}
    if result.returncode == 0:
        try:
            output.update(json.loads(result.stdout.splitlines()[-1]))
        except (ValueError, IndexError):
            output["verified"] = False
    else:
        output["verified"] = False
    return output


def register_python_environment_tools(registry: ToolRegistry, workspace: Workspace, *, tests: bool = False) -> None:
    def inspect_python_environment(package: str) -> dict:
        script = """import sys,json,importlib.metadata as m
try:
 d=m.distribution(sys.argv[1]); data={'installed':True,'version':d.version,'verified':True}
except m.PackageNotFoundError:
 data={'installed':False,'version':None,'verified':False}
print(json.dumps(data))
"""
        return _run(script, package, workspace.root)

    def probe_python_import(module: str) -> dict:
        # 导入有执行副作用，并非强沙箱；只用于用户信任的已安装依赖。
        return _run("import sys,json,importlib; importlib.import_module(sys.argv[1]); print(json.dumps({'imported':sys.argv[1],'verified':True}))", module, workspace.root)

    registry.register(RegisteredTool("inspect_python_environment", "查询当前固定 Python 解释器中已安装发行包版本，不导入目标包", PackageArgs, inspect_python_environment))
    registry.register(RegisteredTool("probe_python_import", "在隔离子进程中导入已安装模块并报告结果；会执行包代码，不验证 Workspace 本地模块", ImportArgs, probe_python_import))
    if tests:
        def run_verification_tests(runner: str = "unittest") -> dict:
            # 复制后验证避免正常测试写入原 Workspace；不是 OS 沙箱。
            with TemporaryDirectory(prefix="tiki-verify-") as directory:
                target = Path(directory)
                total = 0
                count = 0
                for root, dirs, files in os.walk(workspace.root, followlinks=False):
                    dirs[:] = [d for d in dirs if d not in {".git", ".venv", "__pycache__", ".tiki"}
                               and not (Path(root) / d).is_symlink() and not (Path(root) / d).is_junction()]
                    for name in files:
                        source = Path(root) / name
                        if source.is_symlink() or name == ".env" or name.startswith(".env."):
                            continue
                        workspace.resolve(str(source.relative_to(workspace.root)))
                        total += source.stat().st_size
                        count += 1
                        if total > 10_000_000 or count > 1000:
                            raise ToolExecutionError("verification_snapshot_limit", "验证快照超过 10MB 或 1000 文件限制")
                        destination = target / source.relative_to(workspace.root)
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(source, destination)
                script = """import sys,json,unittest,os
sys.path.insert(0,os.getcwd())
if sys.argv[1]=='unittest':
 suite=unittest.defaultTestLoader.discover('.',pattern='test_*.py')
 count=suite.countTestCases(); result=unittest.TextTestRunner(verbosity=2).run(suite)
 print(json.dumps({'test_count':count,'verified':count>0 and result.wasSuccessful()}))
else:
 import pytest
 code=pytest.main(['-q','-p','no:cacheprovider'])
 print(json.dumps({'test_exit_code':int(code),'verified':code==0}))
"""
                return _run(script, runner, target, timeout=30)
        registry.register(RegisteredTool("run_verification_tests", "在临时 Workspace 快照运行固定 unittest/pytest；无测试不算通过，不接受命令参数", TestArgs, run_verification_tests))

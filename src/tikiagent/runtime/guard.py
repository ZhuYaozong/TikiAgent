"""按实际工具调用消耗预算，并阻止相同参数的失败持续重试。"""

from typing import Any
import hashlib
import json
import os
from pathlib import Path

from tikiagent.runtime.models import AgentRunResult, MaxStepsExceeded
from tikiagent.tools.models import ToolResult


class AgentLoopStopped(MaxStepsExceeded):
    """保留停止原因及实际观察，交给 Supervisor 做最终停止决策。"""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.run_result: AgentRunResult | None = None


class ToolLoopGuard:
    def __init__(
        self, *, max_calls: int, repeat_limit: int,
        snapshot: dict[str, Any] | None = None, legacy_calls: int = 0,
        revision_provider=None,
    ) -> None:
        data = snapshot or {}
        # 恢复时采用更严格的配置，不能通过重启扩大已经冻结的预算。
        self.max_calls = min(max_calls, data.get("max_calls", max_calls))
        self.repeat_limit = min(repeat_limit, data.get("repeat_limit", repeat_limit))
        self.calls_used = max(data.get("calls_used", legacy_calls), legacy_calls)
        self.failures: dict[str, int] = dict(data.get("failures", {}))
        self.read_observations: dict[str, dict] = dict(data.get("read_observations", {}))
        self.passed_tests = set(data.get("passed_tests", []))
        self.denials = set(data.get("denials", []))
        self.revision_provider = revision_provider
        self.revision = data.get("revision")

    @staticmethod
    def fingerprint(name: str, arguments_json: str) -> str:
        try:
            arguments = json.loads(arguments_json)
            if isinstance(arguments, dict):
                arguments = dict(arguments)
                # 超时/输出截断并不改变请求的业务内容，不能用改这些字段绕过保护。
                for key in ("timeout_seconds", "output_limit"):
                    arguments.pop(key, None)
                if name == "web_search" and isinstance(arguments.get("query"), str):
                    arguments["query"] = " ".join(arguments["query"].casefold().split())
                if name == "run_command":
                    arguments.setdefault("cwd", ".")
        except ValueError:
            arguments = arguments_json
        value = json.dumps([name, arguments], sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(value.encode()).hexdigest()

    def check(self, name: str, arguments_json: str) -> None:
        if self.revision_provider:
            revision = self.revision_provider()
            if revision != self.revision:
                self.read_observations.clear()
                self.passed_tests.clear()
                self.revision = revision
        if self.calls_used >= self.max_calls:
            raise AgentLoopStopped(
                "tool_budget_exhausted",
                f"实际工具调用已达到预算 {self.max_calls}，停止继续执行",
            )
        key = self.fingerprint(name, arguments_json)
        if key in self.denials:
            raise AgentLoopStopped("permission_denied", "同一操作已被明确拒绝，禁止重复请求或刷新审批")
        if key in self.passed_tests:
            raise AgentLoopStopped("no_progress", "相关工作区未变化且同一测试已通过，无需重复执行")
        if self.failures.get(key, 0) >= self.repeat_limit:
            raise AgentLoopStopped(
                "repeated_tool_failure",
                f"工具 {name} 的相同参数已失败 {self.repeat_limit} 次，停止无进展重试",
            )
        if self.read_observations.get(key, {}).get("repeats", 0) >= self.repeat_limit:
            raise AgentLoopStopped("no_progress", "相同只读请求连续取得相同内容；停止重复取证并总结")

    def record(self, name: str, arguments_json: str, result: ToolResult) -> None:
        self.calls_used += 1
        failed = not result.ok
        if name == "run_command" and isinstance(result.output, dict):
            failed = failed or bool(result.output.get("timed_out")) or result.output.get("exit_code") != 0
        key = self.fingerprint(name, arguments_json)
        if result.error and result.error.code in {"permission_denied", "approval_rejected", "workspace_escape", "tool_not_exposed"}:
            self.denials.add(key)
        if failed:
            self.failures[key] = self.failures.get(key, 0) + 1
        else:
            self.failures.pop(key, None)
        if name in {"read_file", "list_files", "grep"} and not failed:
            digest = hashlib.sha256(json.dumps(result.output, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            prior = self.read_observations.get(key, {})
            self.read_observations[key] = {"digest": digest,
                "repeats": prior.get("repeats", 0) + 1 if prior.get("digest") == digest else 1}
        elif name not in {"read_file", "list_files", "grep", "inspect_python_environment", "probe_python_import"} and not failed:
            # 写入、命令或未知工具可能改变环境；不阻止后续重新检查。
            self.read_observations.clear()
            if not self._test_command(name, arguments_json):
                self.passed_tests.clear()
        if not failed and self._test_command(name, arguments_json):
            self.passed_tests.add(key)
            if self.revision_provider:
                self.revision = self.revision_provider()

    @staticmethod
    def _test_command(name, arguments):
        if name == "run_python_tests":
            return True
        try:
            argv = json.loads(arguments).get("command", [])
        except (ValueError, AttributeError):
            return False
        return name == "run_command" and isinstance(argv, list) and (
            argv[:1] == ["pytest"] or len(argv) >= 3 and argv[1:3] in (["-m", "pytest"], ["-m", "unittest"]))

    def snapshot(self) -> dict[str, Any]:
        return {
            "max_calls": self.max_calls,
            "repeat_limit": self.repeat_limit,
            "calls_used": self.calls_used,
            "failures": dict(self.failures),
            "read_observations": dict(self.read_observations),
            "passed_tests": sorted(self.passed_tests), "denials": sorted(self.denials), "revision": self.revision,
        }


def workspace_revision(root):
    """用有界元数据观察环境变化；不读取文件正文，不跟随链接或 junction。"""
    root = Path(root).resolve()
    entries = []
    for parent, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(name for name in dirs if name not in {".git", "__pycache__", ".pytest_cache"}
            and not (Path(parent) / name).is_symlink()
            and not (hasattr(Path(parent) / name, "is_junction") and (Path(parent) / name).is_junction()))
        for name in sorted(files):
            path = Path(parent) / name
            if path.is_symlink():
                continue
            try:
                path.resolve().relative_to(root)
                stat = path.stat()
            except (ValueError, OSError):
                continue
            entries.append((str(path.relative_to(root)), stat.st_size, stat.st_mtime_ns))
            if len(entries) > 10000:
                # 大目录不做缓存式假设；返回变化值，仍受全局次数限制。
                from uuid import uuid4
                return str(uuid4())
    return hashlib.sha256(json.dumps(entries).encode()).hexdigest()

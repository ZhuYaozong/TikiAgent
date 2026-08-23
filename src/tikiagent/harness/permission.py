"""基于结构化工具参数的确定性 Permission Policy。"""

from collections.abc import Collection
from typing import Protocol

from tikiagent.harness.models import (
    ExecutionContext,
    PermissionDecision,
    ValidatedToolCall,
)


DEFAULT_ALLOWED_TOOLS = frozenset(
    {
        "read_file",
        "list_files",
        "grep",
        "write_file",
        "edit_file",
        "web_search",
        "web_extract",
    }
)


class PermissionPolicy(Protocol):
    """Permission 实现只做策略决定，不调用 handler。"""

    def decide(
        self,
        tool_call: ValidatedToolCall,
        context: ExecutionContext,
    ) -> PermissionDecision: ...


class RuleBasedPermissionPolicy:
    """v0.6a1 默认策略：明确 ALLOW/ASK，其余默认 DENY。"""

    def __init__(
        self,
        *,
        allowed_tools: Collection[str] = DEFAULT_ALLOWED_TOOLS,
    ) -> None:
        self.allowed_tools = frozenset(allowed_tools)

    def decide(
        self,
        tool_call: ValidatedToolCall,
        context: ExecutionContext,
    ) -> PermissionDecision:
        del context  # 第一版规则不按 Agent 分支；Exposure 已经处理能力归属。
        if tool_call.name in self.allowed_tools:
            return PermissionDecision(
                action="ALLOW",
                rule_id=f"tool.{tool_call.name}.allow",
                reason="工具已在默认允许列表；Runtime 仍会强制机械边界",
            )
        if tool_call.name != "run_command":
            return PermissionDecision(
                action="DENY",
                rule_id="tool.unclassified.deny",
                reason="未分类工具默认拒绝",
            )

        command = tool_call.arguments.get("command")
        if not isinstance(command, list) or not all(
            isinstance(item, str) for item in command
        ):
            # 正常情况下 Dispatcher.prepare 已经阻止此分支。
            return PermissionDecision(
                action="DENY",
                rule_id="command.invalid.deny",
                reason="命令不是经过校验的结构化 argv",
            )
        if _is_package_install(command):
            return PermissionDecision(
                action="ASK",
                rule_id="command.package-install.ask",
                reason="安装依赖会改变运行环境，需要外部批准",
            )
        if _is_git_commit(command):
            return PermissionDecision(
                action="ASK",
                rule_id="command.git-commit.ask",
                reason="创建 Git commit 会改变仓库状态，需要外部批准",
            )
        if _is_safe_test(command):
            return PermissionDecision(
                action="ALLOW",
                rule_id="command.test.allow",
                reason="argv 匹配允许的 Python 测试入口",
            )
        return PermissionDecision(
            action="DENY",
            rule_id="command.unclassified.deny",
            reason="argv 不匹配允许或需要批准的命令规则",
        )


class FixedCommandPermissionPolicy:
    """Verifier 等应用节点只允许预先配置的精确 argv。"""

    def __init__(
        self,
        *,
        allowed_commands: Collection[tuple[str, ...]],
        allowed_tools: Collection[str] = ("read_file", "list_files", "grep"),
    ) -> None:
        if not allowed_commands:
            raise ValueError("FixedCommandPermissionPolicy 至少需要一条 argv")
        self.allowed_commands = frozenset(allowed_commands)
        self.allowed_tools = frozenset(allowed_tools)

    def decide(
        self,
        tool_call: ValidatedToolCall,
        context: ExecutionContext,
    ) -> PermissionDecision:
        del context
        if tool_call.name in self.allowed_tools:
            return PermissionDecision(
                action="ALLOW",
                rule_id=f"verifier.{tool_call.name}.allow",
                reason="Verifier 只读工具在应用 allowlist 中",
            )
        command = tool_call.arguments.get("command")
        if tool_call.name == "run_command" and isinstance(command, list):
            if tuple(command) in self.allowed_commands:
                return PermissionDecision(
                    action="ALLOW",
                    rule_id="verifier.fixed-command.allow",
                    reason="argv 与应用预配置验证命令完全一致",
                )
        return PermissionDecision(
            action="DENY",
            rule_id="verifier.unconfigured.deny",
            reason="Verifier 拒绝未预配置的工具或 argv",
        )


def _executable_name(value: str) -> str:
    """同时兼容 Windows 与 POSIX 风格的完整可执行文件路径。"""

    return value.replace("\\", "/").rsplit("/", 1)[-1].lower()


def _is_python(executable: str) -> bool:
    name = _executable_name(executable)
    return name in {"python", "python.exe", "python3", "python3.exe"}


def _is_package_install(argv: list[str]) -> bool:
    if not argv:
        return False
    executable = _executable_name(argv[0])
    if executable in {"pip", "pip.exe", "pip3", "pip3.exe"}:
        return len(argv) >= 2 and argv[1] == "install"
    if _is_python(argv[0]):
        return len(argv) >= 4 and argv[1:4] == ["-m", "pip", "install"]
    if executable in {"uv", "uv.exe"}:
        return (len(argv) >= 2 and argv[1] == "add") or (
            len(argv) >= 3 and argv[1:3] == ["pip", "install"]
        )
    return False


def _is_git_commit(argv: list[str]) -> bool:
    return (
        len(argv) >= 2
        and _executable_name(argv[0]) in {"git", "git.exe"}
        and argv[1] == "commit"
    )


def _is_safe_test(argv: list[str]) -> bool:
    return (
        len(argv) >= 3
        and _is_python(argv[0])
        and argv[1] == "-m"
        and argv[2] in {"pytest", "unittest"}
    )

"""受 Harness 约束的非 Shell 命令执行工具。"""

import subprocess
from time import perf_counter
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from tikiagent.harness.models import CommandResult, ToolExecutionError
from tikiagent.harness.registry import RegisteredTool, ToolRegistry
from tikiagent.harness.workspace import Workspace


class RunCommandArgs(BaseModel):
    """命令参数使用列表表达，避免交给系统 Shell 重新解析。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    command: list[str] = Field(min_length=1)
    cwd: str = "."
    timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    output_limit: int = Field(default=8_000, ge=100, le=100_000)

    @field_validator("command")
    @classmethod
    def validate_command(cls, command: list[str]) -> list[str]:
        if any(not argument for argument in command):
            raise ValueError("command 中不允许出现空参数")
        return command


def _normalize_output(output: str | bytes | None) -> str:
    if output is None:
        return ""
    if isinstance(output, bytes):
        return output.decode("utf-8", errors="replace")
    return output


def _limit_output(output: str, limit: int) -> tuple[str, bool]:
    if len(output) <= limit:
        return output, False

    marker = "\n...[output truncated]"
    remaining = max(0, limit - len(marker))
    return output[:remaining] + marker, True


def register_command_tool(
    registry: ToolRegistry,
    workspace: Workspace,
) -> None:
    """向现有注册表加入绑定到指定 Workspace 的命令工具。"""

    def run_command(
        command: list[str],
        cwd: str = ".",
        timeout_seconds: float = 30.0,
        output_limit: int = 8_000,
    ) -> dict[str, Any]:
        working_directory = workspace.require_directory(cwd)
        started_at = perf_counter()

        try:
            completed = subprocess.run(
                command,
                cwd=working_directory,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                shell=False,
                check=False,
            )
        except FileNotFoundError as error:
            raise ToolExecutionError(
                code="command_not_found",
                message="找不到命令",
                details={"command": command[0]},
            ) from error
        except subprocess.TimeoutExpired as error:
            duration = perf_counter() - started_at
            stdout, stdout_truncated = _limit_output(
                _normalize_output(error.stdout),
                output_limit,
            )
            stderr, stderr_truncated = _limit_output(
                _normalize_output(error.stderr),
                output_limit,
            )
            return CommandResult(
                command=command,
                cwd=cwd,
                exit_code=None,
                stdout=stdout,
                stderr=stderr,
                duration_seconds=duration,
                timed_out=True,
                stdout_truncated=stdout_truncated,
                stderr_truncated=stderr_truncated,
            ).model_dump()

        duration = perf_counter() - started_at
        stdout, stdout_truncated = _limit_output(
            completed.stdout,
            output_limit,
        )
        stderr, stderr_truncated = _limit_output(
            completed.stderr,
            output_limit,
        )
        return CommandResult(
            command=command,
            cwd=cwd,
            exit_code=completed.returncode,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=duration,
            timed_out=False,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
        ).model_dump()

    registry.register(
        RegisteredTool(
            name="run_command",
            description=(
                "在 Workspace 内的 cwd 启动非 Shell 命令。command 必须是"
                "参数列表，例如 ['python', '-m', 'unittest']。返回"
                " exit_code、stdout、stderr、duration_seconds 和 timed_out；"
                "只有 timed_out=false 且 exit_code=0 才表示命令成功。"
            ),
            args_model=RunCommandArgs,
            handler=run_command,
        )
    )

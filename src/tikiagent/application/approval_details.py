"""将审批请求转换为可供用户判断的显示数据；保留原请求不变。"""

from __future__ import annotations

from typing import Any
import re

from tikiagent.application.models import ApprovalDetails
from tikiagent.harness.permissions.models import ApprovalRequest


_SECRET_MARKERS = ("api_key", "apikey", "authorization", "cookie", "password", "secret", "token")
_ASSIGNMENT = re.compile(
    r"(?i)((?:api[_-]?key|authorization|cookie|password|secret|token)\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;&]+)"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_HEADER = re.compile(r"(?i)((?:authorization|cookie)\s*[:=]\s*)[^\r\n]*")
_API_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}")
_URL_CREDENTIALS = re.compile(r"(https?://)[^/\s@]+@", re.IGNORECASE)
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")


def _is_secret(key: str) -> bool:
    normalized = key.casefold().replace("-", "_")
    return any(marker in normalized for marker in _SECRET_MARKERS)


def _safe_text(value: str) -> str:
    # 静态组件使用普通文本；额外转义终端控制字符，避免参数改变显示效果。
    value = _HEADER.sub(lambda match: match[1] + "[REDACTED]", value)
    value = _BEARER.sub("Bearer [REDACTED]", value)
    value = _ASSIGNMENT.sub(lambda match: match[1] + "[REDACTED]", value)
    value = _API_KEY.sub("[REDACTED]", value)
    value = _URL_CREDENTIALS.sub(lambda match: match[1] + "[REDACTED]@", value)
    return _CONTROL.sub(lambda match: f"\\u{ord(match[0]):04x}", value)


def _display_value(value: Any, key: str | None = None) -> Any:
    if key is not None and _is_secret(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {_safe_text(str(k)): _display_value(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        if key == "command":
            # --token VALUE 和 --token=VALUE 都要遮蔽；不改变 argv 的数量或顺序。
            result = []
            hide_next = False
            for argument in value:
                if hide_next:
                    result.append("[REDACTED]")
                    hide_next = False
                    continue
                result.append(_display_value(argument))
                if isinstance(argument, str) and argument.startswith("-"):
                    hide_next = "=" not in argument and _is_secret(argument)
            return result
        return [_display_value(item) for item in value]
    if isinstance(value, str):
        return _safe_text(value)
    return value


def build_approval_details(request: ApprovalRequest) -> ApprovalDetails:
    """从规范化参数读取显示值；不经事件截断，也不把脱敏值回写 Checkpoint。"""

    return ApprovalDetails(
        request_id=request.request_id,
        tool_call_id=request.tool_call.tool_call_id,
        tool_name=request.tool_call.name,
        session_id=request.scope.session_id,
        task_id=request.scope.task_id,
        workspace_id=request.scope.workspace_id,
        reason=_safe_text(request.permission.reason),
        rule_id=request.permission.rule_id,
        arguments=_display_value(request.tool_call.arguments),
    )


def safe_display_text(value: str) -> str:
    """事件与审批共用内嵌凭据/终端控制字符脱敏，不改变执行参数。"""
    return _safe_text(value)


def safe_display_command(value: list | tuple) -> list:
    """遮蔽 argv 中独立的 --token VALUE 等凭据。"""
    return _display_value(value, "command")

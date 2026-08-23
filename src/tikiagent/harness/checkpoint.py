"""Checkpoint：恢复执行事实的唯一权威来源。"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tikiagent.harness.models import (
    ApprovalRequest,
    ExecutionScope,
    ToolResult,
    ValidatedToolCall,
)


ApprovalState = Literal[
    "not_required",
    "awaiting",
    "granted",
    "rejected",
]
ExecutionState = Literal[
    "pending",
    "awaiting_approval",
    "executing",
    "completed",
    "denied",
    "recovery_required",
    "awaiting_reconcile",
]


class CheckpointError(RuntimeError):
    """Checkpoint 基础异常。"""


class CheckpointNotFoundError(CheckpointError):
    """指定 Checkpoint 不存在。"""


class CheckpointConflictError(CheckpointError):
    """revision 不匹配或重复创建。"""


class CheckpointIntegrityError(CheckpointError):
    """Checkpoint 内容损坏或被意外改动。"""


class ExecutionIdentity(BaseModel):
    """一次真实 handler 执行尝试的身份。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    run_id: str = Field(min_length=1)
    execution_id: str = Field(min_length=1)
    tool_call_id: str = Field(min_length=1)
    attempt: int = Field(default=1, ge=1)


class HistoryResumeReference(BaseModel):
    """跨进程重新连接 History Store 所需的稳定引用。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    backend: Literal["jsonl"] = "jsonl"
    path: str = Field(min_length=1)
    cursor: int = Field(ge=0)


class WorkflowResumeSnapshot(BaseModel):
    """Graph 重入所需的工作流状态，不依赖 Trace 推断。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    state: dict[str, Any]
    resume_target: Literal["code_agent"] = "code_agent"
    history: HistoryResumeReference


class PendingModelToolCall(BaseModel):
    """保留供应商原始 JSON 参数，恢复时仍按原顺序校验。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    tool_call_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    arguments_json: str


class ReActRunSnapshot(BaseModel):
    """单次 Specialist ReAct Loop 的短期执行快照。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    task: str = Field(min_length=1)
    step: int = Field(ge=1)
    base_context: dict[str, Any]
    local_memory: dict[str, Any]
    tool_results: list[ToolResult] = Field(default_factory=list)
    context_usages: list[dict[str, Any]] = Field(default_factory=list)
    phases: list[str] = Field(default_factory=list)
    pending_assistant_message: dict[str, Any]
    pending_tool_calls: list[PendingModelToolCall] = Field(min_length=1)
    pending_results: list[ToolResult] = Field(default_factory=list)
    next_tool_index: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_ordered_progress(self) -> "ReActRunSnapshot":
        if self.next_tool_index != len(self.pending_results):
            raise ValueError("next_tool_index 必须等于已完成的顺序 ToolResult 数量")
        if self.next_tool_index >= len(self.pending_tool_calls):
            raise ValueError("Checkpoint 只能停在尚未完成的 ToolCall 上")
        expected_ids = [
            call.tool_call_id
            for call in self.pending_tool_calls[: self.next_tool_index]
        ]
        actual_ids = [result.tool_call_id for result in self.pending_results]
        if expected_ids != actual_ids:
            raise ValueError("pending_results 必须严格对应前 N 个 ToolCall")
        return self


class ExecutionCheckpoint(BaseModel):
    """一个原子 Checkpoint 同时保存 Workflow 与 ReAct 两层快照。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[1] = 1
    checkpoint_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)
    revision: int = Field(default=1, ge=1)
    identity: ExecutionIdentity
    scope: ExecutionScope
    agent: str = Field(min_length=1)
    exposed_tools: set[str]
    tool_call: ValidatedToolCall
    approval_state: ApprovalState
    execution_state: ExecutionState
    approval_request: ApprovalRequest | None = None
    tool_result: ToolResult | None = None
    workflow_snapshot: WorkflowResumeSnapshot
    react_snapshot: ReActRunSnapshot
    recovery_note: str | None = None
    reconcile_evidence: list[str] = Field(default_factory=list)
    manually_reconciled: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_lifecycle(self) -> "ExecutionCheckpoint":
        if self.identity.tool_call_id != self.tool_call.tool_call_id:
            raise ValueError("ExecutionIdentity 必须绑定当前 ToolCall")
        if self.execution_state == "awaiting_approval":
            if self.approval_state != "awaiting" or self.approval_request is None:
                raise ValueError("awaiting_approval 必须保存待处理 ApprovalRequest")
        if self.execution_state == "executing" and self.approval_state not in {
            "not_required",
            "granted",
        }:
            raise ValueError("executing 必须已经无需审批或获得批准")
        if self.execution_state == "completed" and self.tool_result is None:
            raise ValueError("completed 必须保存真实 ToolResult")
        if self.execution_state == "awaiting_reconcile" and self.tool_result is not None:
            raise ValueError("awaiting_reconcile 禁止伪造 ToolResult")
        if self.manually_reconciled and (
            self.execution_state != "completed"
            or self.tool_result is None
            or not self.reconcile_evidence
        ):
            raise ValueError("人工 reconcile 必须提供 Result、证据并完成执行")
        return self


class _CheckpointEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    checksum: str = Field(min_length=64, max_length=64)
    payload: dict[str, Any]


class JsonCheckpointStore:
    """使用临时文件 + fsync + os.replace 原子保存 JSON Checkpoint。"""

    _SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()

    def save(
        self,
        checkpoint: ExecutionCheckpoint,
        *,
        expected_revision: int | None = None,
    ) -> ExecutionCheckpoint:
        """创建或 CAS 更新；revision 防止重复 Resume。"""

        path = self._path(checkpoint.checkpoint_id)
        with self._lock:
            if path.exists():
                existing = self.load(checkpoint.checkpoint_id)
                if expected_revision is None:
                    raise CheckpointConflictError("更新 Checkpoint 必须提供 expected_revision")
                if existing.revision != expected_revision:
                    raise CheckpointConflictError(
                        f"Checkpoint revision 冲突：expected={expected_revision}, "
                        f"actual={existing.revision}"
                    )
                if checkpoint.revision != expected_revision + 1:
                    raise CheckpointConflictError("新 revision 必须恰好增加 1")
            elif expected_revision is not None or checkpoint.revision != 1:
                raise CheckpointConflictError("新 Checkpoint 必须从 revision=1 创建")

            payload = checkpoint.model_dump(mode="json")
            canonical = _canonical_json(payload)
            envelope = {
                "checksum": hashlib.sha256(canonical).hexdigest(),
                "payload": payload,
            }
            temporary = path.with_suffix(path.suffix + f".{uuid4().hex}.tmp")
            try:
                with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                    json.dump(envelope, stream, ensure_ascii=False, sort_keys=True)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                if temporary.exists():
                    temporary.unlink()
        return checkpoint

    def load(self, checkpoint_id: str) -> ExecutionCheckpoint:
        path = self._path(checkpoint_id)
        if not path.exists():
            raise CheckpointNotFoundError(f"Checkpoint 不存在：{checkpoint_id}")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            envelope = _CheckpointEnvelope.model_validate(raw)
        except (OSError, ValueError) as error:
            raise CheckpointIntegrityError("Checkpoint JSON 无法解析") from error
        if hashlib.sha256(_canonical_json(envelope.payload)).hexdigest() != envelope.checksum:
            raise CheckpointIntegrityError("Checkpoint checksum 不匹配")
        try:
            # JSON 表示会把 set/datetime 编码为 list/string；schema 负责还原类型。
            return ExecutionCheckpoint.model_validate(
                envelope.payload,
                strict=False,
            )
        except ValueError as error:
            raise CheckpointIntegrityError("Checkpoint schema 校验失败") from error

    def _path(self, checkpoint_id: str) -> Path:
        if not self._SAFE_ID.fullmatch(checkpoint_id):
            raise ValueError("checkpoint_id 只能包含字母、数字、点、横线和下划线")
        return self.root / f"{checkpoint_id}.json"


def next_checkpoint(
    checkpoint: ExecutionCheckpoint,
    **updates: Any,
) -> ExecutionCheckpoint:
    """通过完整重校验产生 revision+1 的下一状态。"""

    payload = checkpoint.model_dump(mode="python")
    payload.update(updates)
    payload["revision"] = checkpoint.revision + 1
    payload["updated_at"] = datetime.now(UTC)
    return ExecutionCheckpoint.model_validate(payload)


def new_execution_identity(
    *,
    run_id: str,
    tool_call_id: str,
    attempt: int = 1,
) -> ExecutionIdentity:
    return ExecutionIdentity(
        run_id=run_id,
        execution_id=str(uuid4()),
        tool_call_id=tool_call_id,
        attempt=attempt,
    )


def _canonical_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

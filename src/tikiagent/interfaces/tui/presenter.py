"""将安全事件投影为紧凑摘要；不展示模型推理或完整执行原文。"""

from __future__ import annotations

import json
from typing import Any

from tikiagent.application.models import ApplicationEvent
from tikiagent.interfaces.tui.models import FeedItem, FeedKind


REVIEW_LABELS = {"accept": "已接受", "accept_with_limitations": "含限制", "request_changes": "需补做", "stop": "已停止"}
TODO_LABELS = {"pending": "待执行", "in_progress": "执行中", "awaiting_verification": "待审核", "awaiting_review": "待验收", "completed": "已完成", "failed": "需处理"}


def clipped(value: Any, limit: int = 600) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "…（显示已截断）"


def tool_key(event: ApplicationEvent) -> str:
    """作用域 + 执行 Agent + Run + ToolCall，避免同名工具/其他任务串卡。"""
    agent = event.data.get("agent") or ("code_agent" if event.source == "execution_harness_adapter" else event.source)
    return json.dumps([event.scope.session_id, event.scope.task_id, agent, event.scope.run_id, event.correlation_id])


def tool_label(status: str, target: str, result: str) -> str:
    labels = {"requested": "待执行", "running": "执行中", "completed": "执行完成", "failed": "工具失败",
              "denied": "已拒绝", "nonzero": "命令非零退出", "timeout": "超时", "approval": "待审批", "recovery": "需人工恢复"}
    # 结果前置，避免长 argv 把退出码/失败原因挤出折叠标题。
    return " · ".join(x for x in (labels.get(status, status), result, target) if x)


class TuiEventPresenter:
    def present(self, event: ApplicationEvent) -> FeedItem | None:
        kind, data = event.event_type, event.data
        if kind == "turn_received":
            return self._item(event, "user", "You", event.message, collapsed=False)
        if kind == "final_answer":
            return self._item(event, "error" if data.get("error_category") else "assistant", "TikiAgent",
                              _first_line(event.message), detail=event.message, collapsed=False)
        if kind in {"intent_routed", "supervisor_decision"}:
            return self._item(event, "routing", "Intent Router" if kind == "intent_routed" else "Supervisor", event.message)
        if kind == "context_prepared":
            if not data.get("actions") and not data.get("compression") and not data.get("compression_events"):
                return None
            before, after = data.get("before", {}), data.get("after", {})
            return self._item(event, "agent", "Context", f"上下文估算：{before.get('total_call_usage', '?')} → {after.get('total_call_usage', '?')}",
                detail=clipped({"before": before, "after": after, "actions": data.get("actions"), "compression_events": data.get("compression_events")}, 2000))
        if kind == "agent_started":
            return self._item(event, "agent", _agent_name(str(data.get("agent") or event.message.removeprefix("进入 "))), "开始执行")
        if kind == "handoff_created":
            return self._item(event, "agent", "Handoff", event.message + (f" · Todo {data['todo_id']}" if data.get("todo_id") else ""),
                detail=clipped(data.get("instruction", ""), 1500) or None)
        if kind == "specialist_result":
            details = [clipped(data.get("summary", ""), 1200)]
            if data.get("changed_files"):
                details.append("交付文件：" + clipped(data["changed_files"]))
            if data.get("sources"):
                details.append("来源：" + clipped(data["sources"], 800))
            return self._item(event, "agent", "Specialist Result", f"{_agent_name(data.get('agent', 'specialist'))} · {data.get('delivery_status') or '返回结果'}",
                              detail="\n".join(details)[:2400] or None)
        if kind == "verification_completed":
            passed = data.get("passed") is True
            detail = verification_detail(data)
            if data.get("advisory"):
                return self._item(event, "verification", "Verifier · 审核意见",
                    "符合条件 · 待 Supervisor 决定" if passed else "存在缺口 · 待 Supervisor 决定",
                    detail=detail or event.message, collapsed=False)
            return self._item(event, "verification" if passed else "error", "Verification", "PASS" if passed else "FAIL", detail=detail or event.message, collapsed=False)
        if kind == "result_reviewed":
            labels = {"accept": "接受交付", "accept_with_limitations": "带限制接受", "request_changes": "要求补做", "stop": "停止"}
            detail = "\n".join([str(data.get("reason", "")), *data.get("limitations", [])])
            return self._item(event, "routing", "Supervisor · 验收决定", labels.get(data.get("action"), "验收决定"),
                              detail=clipped(detail, 2000), collapsed=False)
        if kind in {"tool_call_requested", "tool_execution_started", "tool_result_received"}:
            return self._tool_item(event)
        if kind in {"approval_required", "approval_decided"}:
            return self._item(event, "approval", "Approval", event.message, collapsed=False)
        if kind in {"recovery_required", "recovery_decided", "reconciliation_completed", "workflow_denied", "workflow_failed"}:
            return self._item(event, "error", "Recovery" if "recover" in kind or "reconcil" in kind else "Workflow", event.message, collapsed=False)
        # TaskBoard 通过独立只读卡片和侧栏更新，不为每次变化追加噪声。
        return None

    def _tool_item(self, event: ApplicationEvent) -> FeedItem:
        data = event.data
        result = data.get("tool_result") or {}
        name = data.get("tool_name") or result.get("tool_name") or event.message.split(":", 1)[0].strip()
        args = data.get("arguments") or {}
        target = _target(args) if isinstance(args, dict) else clipped(args, 100)
        status = {"tool_call_requested": "requested", "tool_execution_started": "running", "tool_result_received": "completed"}[event.event_type]
        outcome, detail = _tool_output(name, result)
        if result.get("ok") is False:
            code = (result.get("error") or {}).get("code", "tool_error")
            status = "denied" if code in {"permission_denied", "tool_not_exposed", "workspace_escape", "approval_denied"} else "failed"
        output = result.get("output")
        if result.get("ok") is True and isinstance(output, dict):
            if output.get("timed_out"):
                status = "timeout"
            elif output.get("exit_code") not in (None, 0):
                status = "nonzero"
        request = "参数：\n" + clipped(args, 1000) if args else ""
        detail = "\n".join(x for x in (request, detail) if x)
        item = self._item(event, "tool", f"{_agent_name(data.get('agent') or ('code_agent' if event.source == 'execution_harness_adapter' else 'agent'))} · {name}",
            tool_label(status, target, outcome), detail=detail or None, collapsed=status not in {"failed", "denied", "nonzero", "timeout"})
        return item.model_copy(update={"card_key": "tool:" + tool_key(event), "call_key": tool_key(event),
            "execution_id": data.get("execution_id"), "attempt": data.get("attempt"),
            "tool_state": status, "target": target, "request_detail": request, "result_detail": outcome})

    @staticmethod
    def _item(event: ApplicationEvent, kind: FeedKind, title: str, summary: str, *, detail=None, collapsed=True) -> FeedItem:
        return FeedItem(sequence=event.sequence, kind=kind, title=title, summary=summary or "-", detail=detail, collapsed=collapsed,
                        card_key=event.event_id)


def _target(args: dict) -> str:
    for key in ("command", "query", "url", "path", "todo_id", "pattern"):
        if key in args:
            value = args[key]
            if key == "command" and isinstance(value, list):
                value = " ".join(str(v) for v in value)
            target = f"{value}"
            if key == "command":
                target += f" · cwd={args.get('cwd', '.')}"
            if args.get("pattern") and key == "path":
                target += f" · {args['pattern']}"
            return clipped(target, 160)
    return ""


def _tool_output(name: str, result: dict) -> tuple[str, str]:
    if not result:
        return "", ""
    error = result.get("error")
    if isinstance(error, dict):
        return str(error.get("code", "tool_error")), clipped(error, 1200)
    output = result.get("output")
    if not isinstance(output, dict):
        return clipped(output, 100), clipped(output)
    if name == "read_file":
        text = str(output.get("content", ""))
        count = output.get("content_characters")
        size = f"{count} 字符" if count is not None else "内容预览"
        return f"{output.get('path', '')} · {size}", "内容预览：\n" + clipped(text, 800)
    if name == "list_files":
        files = output.get("files", [])
        return f"{output.get('path', '.')} · 返回 {output.get('files_count', len(files))} 项", "文件：\n" + clipped(files, 1200)
    if name == "grep":
        matches = output.get("matches", [])
        return f"匹配 {output.get('matches_count', len(matches))} 项", "匹配预览：\n" + clipped(matches[:8], 1200)
    if name == "web_search":
        sources = output.get("results", [])
        preview = [{k: x.get(k) for k in ("title", "url", "snippet")} for x in sources[:5] if isinstance(x, dict)]
        return f"返回 {output.get('results_count', len(sources))} 个来源", "来源：\n" + clipped(preview, 1600)
    if name == "web_extract":
        return clipped(output.get("url", "提取完成"), 100), "正文预览：\n" + clipped(output.get("content", ""), 1000)
    if name == "run_command":
        summary = f"exit={output.get('exit_code')} · {output.get('duration_seconds', '?')}s"
        details = {k: output[k] for k in ("command", "cwd", "exit_code", "timed_out", "stdout", "stderr", "stdout_truncated", "stderr_truncated", "display_truncated") if k in output}
        return summary, clipped(details, 1600)
    if name in {"write_file", "edit_file"}:
        return " · ".join(f"{k}={output[k]}" for k in ("path", "characters_written", "replacements") if k in output), clipped(output, 900)
    if name == "submit_verification":
        return "已提交审核意见", verification_detail(output) or clipped(output, 1600)
    # 未知工具仍有有界结果摘要，不回退成无内容的 completed 卡片。
    return _first_line(clipped(output, 140)), clipped(output, 1200)


def verification_detail(data: dict) -> str:
    lines = []
    if data.get("todo_id"):
        lines.append("Todo：" + str(data["todo_id"]))
    for item in data.get("assessments", [])[:12]:
        lines.append(f"{item.get('criterion_id')}: {item.get('status')} · {item.get('reason', '')}")
        for ref in item.get("evidence_refs", [])[:3]:
            lines.append(f"证据 {ref}: " + clipped(data.get("evidence_records", {}).get(ref, {}), 240))
    for item in data.get("checks", [])[:12]:
        lines.append(f"{item.get('name')}: {'通过' if item.get('passed') else '未通过'} · {item.get('evidence', '')}")
    lines.extend(str(x) for x in data.get("failures", []))
    if data.get("recommendation"):
        lines.append("建议：" + str(data["recommendation"]))
    return clipped("\n".join(lines), 2400)


def _agent_name(value: str) -> str:
    return {"supervisor": "Supervisor", "research_agent": "ResearchAgent", "code_agent": "CodeAgent",
            "verification_gate": "Verifier · 审核", "verifier": "VerifierAgent", "resume_entry": "Resume · 恢复入口"}.get(value, value.replace("_", " ").title())


def _first_line(value: str, limit: int = 120) -> str:
    return clipped(next((item.strip() for item in value.splitlines() if item.strip()), "完成"), limit)

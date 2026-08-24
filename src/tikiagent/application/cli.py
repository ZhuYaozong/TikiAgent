"""TikiAgent v0.7 命令行入口。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pydantic import ValidationError

from tikiagent.application.events import CliEventSink, EventBus
from tikiagent.application.models import ReconcileSubmission
from tikiagent.application.runtime import ApplicationRuntimeFactory


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TikiAgent Multi-Agent CLI")
    parser.add_argument("--data-dir", type=Path, default=Path(".tiki"))
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--json", action="store_true", help="只输出最终 JSON")
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("new-session", help="创建会话")
    create.add_argument("--workspace-id", default="default-workspace")

    submit = commands.add_parser("submit", help="提交一轮用户输入")
    submit.add_argument("--session-id", required=True)
    submit.add_argument("--message", required=True)

    status = commands.add_parser("status", help="读取权威 Checkpoint 状态")
    status.add_argument("--session-id", required=True)

    resume = commands.add_parser("resume", help="处理 Approval 后恢复 Graph")
    resume.add_argument("--session-id", required=True)
    resume.add_argument("--request-id", required=True)
    resume.add_argument("--expected-revision", required=True, type=int)
    choice = resume.add_mutually_exclusive_group(required=True)
    choice.add_argument("--approve", action="store_true")
    choice.add_argument("--deny", action="store_true")

    recover = commands.add_parser("recover", help="处理未知副作用执行状态")
    recover.add_argument("--session-id", required=True)
    recover.add_argument("--execution-id", required=True)
    recover.add_argument("--expected-revision", required=True, type=int)
    recover.add_argument(
        "--action",
        required=True,
        choices=("confirmed_not_executed", "confirmed_executed"),
    )
    recover.add_argument("--decided-by", required=True)
    recover.add_argument("--reason", required=True)

    reconcile = commands.add_parser("reconcile", help="提交真实人工核对结果")
    reconcile.add_argument("--session-id", required=True)
    reconcile.add_argument("--execution-id", required=True)
    reconcile.add_argument("--expected-revision", required=True, type=int)
    reconcile.add_argument("--result-file", required=True, type=Path)
    return parser


def execute(args: argparse.Namespace):
    bus = EventBus()
    if not args.json:
        bus.subscribe(CliEventSink())
    controller = ApplicationRuntimeFactory(
        args.data_dir,
        env_file=args.env_file,
        event_bus=bus,
    ).build_controller()
    if args.command == "new-session":
        return controller.new_session(workspace_id=args.workspace_id)
    if args.command == "submit":
        return controller.submit(
            session_id=args.session_id,
            user_input=args.message,
        )
    if args.command == "status":
        return controller.status(session_id=args.session_id)
    if args.command == "resume":
        return controller.resume(
            session_id=args.session_id,
            expected_revision=args.expected_revision,
            request_id=args.request_id,
            approved=args.approve,
        )
    if args.command == "recover":
        return controller.recover(
            session_id=args.session_id,
            expected_revision=args.expected_revision,
            execution_id=args.execution_id,
            action=args.action,
            decided_by=args.decided_by,
            reason=args.reason,
        )
    if args.command == "reconcile":
        submission = ReconcileSubmission.model_validate_json(
            args.result_file.read_text(encoding="utf-8")
        )
        return controller.reconcile(
            session_id=args.session_id,
            expected_revision=args.expected_revision,
            execution_id=args.execution_id,
            submission=submission,
        )
    raise RuntimeError(f"未知命令：{args.command}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        outcome = execute(args)
    except (RuntimeError, OSError, ValidationError, ValueError) as error:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": type(error).__name__,
                        "message": str(error),
                    },
                },
                ensure_ascii=False,
            )
        )
        return 1
    print(outcome.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

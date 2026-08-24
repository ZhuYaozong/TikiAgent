"""三类主 Demo 的命令行入口。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pydantic import ValidationError

from tikiagent.demo.runner import DemoRunner
from tikiagent.demo.scenarios import get_scenario


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TikiAgent Demo Validation")
    parser.add_argument("scenario", choices=("research", "coding", "hybrid"))
    parser.add_argument("--data-dir", type=Path, default=Path(".tiki-demo"))
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--workspace-id", default="demo-workspace")
    parser.add_argument("--dry-run", action="store_true", help="仅查看场景，不调用模型")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    scenario = get_scenario(args.scenario)
    if args.dry_run:
        if args.json:
            print(scenario.model_dump_json(indent=2))
        else:
            print(f"[{scenario.title}]\n{scenario.task}")
        return 0

    try:
        result = DemoRunner(
            args.data_dir,
            env_file=args.env_file,
        ).run(scenario, workspace_id=args.workspace_id)
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

    if args.json:
        print(result.summary.model_dump_json(indent=2))
    else:
        print(f"Demo status: {result.summary.status}")
        print(f"Result: {result.output_dir / 'demo-result.md'}")
        if result.summary.next_action:
            print(f"Next: {result.summary.next_action}")
    if result.summary.status in {
        "awaiting_approval",
        "recovery_required",
        "awaiting_reconcile",
    }:
        return 2
    return 0 if result.summary.status == "workflow_completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

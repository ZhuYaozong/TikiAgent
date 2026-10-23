"""独立进程执行图或在指定事务窗口等待父进程终止。"""
import asyncio
import json
import sys
from pathlib import Path

from langgraph.checkpoint.base import empty_checkpoint
from langgraph.types import Command

from fenced_saver import FencedSaver, LeaseLost
from graphs import build
from lease_store import claim


async def main():
    spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    path = Path(spec["output"])
    if spec["mode"] == "claim":
        epoch = await claim(spec["thread"], spec["owner"])
        path.write_text(json.dumps({"epoch": epoch}), encoding="utf-8")
        return
    saver = FencedSaver(spec["thread"], spec["owner"], spec["epoch"])
    config = {"configurable": {"thread_id": spec["thread"], "checkpoint_ns": ""}}
    if spec["mode"] in ("hold", "late", "crash", "crash_writes"):
        async def barrier():
            Path(spec["ready"]).touch()
            while not Path(spec["release"]).exists():
                await asyncio.sleep(0.02)
        if spec["mode"] in ("crash", "crash_writes"):
            saver.before_commit = barrier
        elif spec["mode"] == "hold":
            saver.after_lock = barrier
        else:
            await barrier()
        try:
            if spec["mode"] == "crash_writes":
                config["configurable"]["checkpoint_id"] = spec["checkpoint_id"]
                await saver.aput_writes(config, [("values", ["poison"])], "crashed")
            else:
                cp = empty_checkpoint()
                cp["channel_values"] = {"values": ["poison"]}
                cp["channel_versions"] = {"values": "2"}
                await saver.aput(config, cp, {"source": "input", "step": 0, "parents": {}}, {"values": "2"})
            result = {"saved": True}
        except LeaseLost:
            result = {"rejected": True}
    else:
        graph = build(saver, spec["mode"], Path(spec["trace"]), spec.get("fail", False))
        data = Command(resume="approved") if spec.get("resume") and spec["mode"] == "interrupt" else None if spec.get("resume") else {"values": []}
        try:
            result = await graph.ainvoke(data, config)
            result.pop("__interrupt__", None)
        except RuntimeError as exc:
            if str(exc) != "injected-node-failure":
                raise
            result = {"expected_failure": True}
    path.write_text(json.dumps(result), encoding="utf-8")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())

"""真实数据库与独立 worker 故障验证；只输出脱敏断言结果。"""
import asyncio
import importlib.metadata as md
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
from uuid import uuid4

from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from fenced_saver import FencedSaver, LeaseLost, recovery_backend, scoped_thread
from lease_store import claim, connect, validated_dsn

ROOT = Path(__file__).resolve().parent
WORK = ROOT / ".runtime" / uuid4().hex
CASES = []
PROCESSES = []


def check(name, passed):
    if not passed:
        raise AssertionError(name)
    CASES.append({"case": name, "passed": True})
    print("PASS", name, flush=True)


def spawn(**spec):
    key = uuid4().hex
    spec["output"] = str(WORK / (key + ".result.json"))
    request = WORK / (key + ".json")
    request.write_text(json.dumps(spec), encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(ROOT / "worker.py"), str(request)])
    PROCESSES.append(proc)
    return proc, Path(spec["output"])


async def finish(item):
    proc, output = item
    code = await asyncio.to_thread(proc.wait, 20)
    if code:
        raise RuntimeError(f"worker exit={code}")
    return json.loads(output.read_text(encoding="utf-8"))


async def wait_file(path):
    end = time.monotonic() + 15
    while not path.exists():
        if time.monotonic() > end:
            raise TimeoutError("worker barrier")
        await asyncio.sleep(0.02)


async def expire(thread):
    async with await connect() as conn:
        await conn.execute("UPDATE spike_leases SET expires_at=clock_timestamp()-interval '1 second' WHERE thread_id=%s", (thread,))


async def wait_database(sql, args=()):
    end = time.monotonic() + 15
    async with await connect() as conn:
        while time.monotonic() < end:
            row = await (await conn.execute(sql, args)).fetchone()
            if row and row["ready"]:
                return
            await asyncio.sleep(0.05)
    raise TimeoutError("数据库同步条件未满足")


def config(thread):
    return {"configurable": {"thread_id": thread, "checkpoint_ns": ""}}


async def put(saver, thread):
    cp = empty_checkpoint()
    cp["channel_values"] = {"values": ["safe"]}
    cp["channel_versions"] = {"values": "1"}
    return await saver.aput(config(thread), cp, {"source": "input", "step": 0, "parents": {}}, {"values": "1"})


async def rejected(name, coro, error=LeaseLost):
    try:
        await coro
    except error:
        check(name, True)
    else:
        check(name, False)


async def main():
    WORK.mkdir(parents=True)
    for invalid in ("postgresql://b04:disposable-b04@127.0.0.1:5432/b04_checkpoint", "postgresql://b04:disposable-b04@127.0.0.1:38445/production"):
        os.environ["B04_TEST_DATABASE_URL"] = invalid
        try:
            validated_dsn()
        except ValueError:
            pass
        else:
            raise AssertionError("unsafe DSN")
    os.environ.pop("B04_TEST_DATABASE_URL", None)
    check("reject_non_test_database_before_connect", True)
    async with await connect() as conn:
        await AsyncPostgresSaver(conn).setup()
        await conn.execute((ROOT / "schema.sql").read_text(encoding="utf-8"))

    for mode in ("interrupt", "parallel"):
        thread = scoped_thread("alice", mode)
        epoch = await claim(thread, "first")
        trace = WORK / (mode + ".trace")
        first = await finish(spawn(mode=mode, thread=thread, owner="first", epoch=epoch, trace=str(trace), fail=mode == "parallel"))
        check(mode + "_first_process", first == ({"values": []} if mode == "interrupt" else {"expected_failure": True}))
        old = FencedSaver(thread, "first", epoch)
        saved = await old.aget_tuple(config(thread))
        check(mode + "_checkpoint_persisted", saved is not None)
        if mode == "parallel":
            check("successful_sibling_pending_writes", any(w[1] == "values" and w[2] == ["good"] for w in saved.pending_writes))
        await expire(thread)
        epoch = await claim(thread, "second")
        resumed = await finish(spawn(mode=mode, thread=thread, owner="second", epoch=epoch, trace=str(trace), resume=True))
        check(mode + "_new_process_resume", sorted(resumed["values"]) == (["approved"] if mode == "interrupt" else ["flaky", "good"]))
        counts = trace.read_text(encoding="utf-8").splitlines()
        check(mode + "_node_replay_behavior", counts.count("waiting") == 2 if mode == "interrupt" else counts.count("good") == 1 and counts.count("flaky") == 2)
        await rejected(mode + "_old_epoch_read_rejected", old.aget_tuple(config(thread)))

    thread = scoped_thread("alice", "race")
    epoch = await claim(thread, "A")
    saver = FencedSaver(thread, "A", epoch)
    saved_config = await put(saver, thread)
    check("explicit_checkpoint_read", (await saver.aget_tuple(saved_config)).checkpoint["channel_values"]["values"] == ["safe"])
    ready, release = WORK / "late.ready", WORK / "late.release"
    late = spawn(mode="late", thread=thread, owner="A", epoch=epoch, ready=str(ready), release=str(release))
    await wait_file(ready)
    await expire(thread)
    claims = [spawn(mode="claim", thread=thread, owner=owner) for owner in ("B", "C")]
    winners = [await finish(item) for item in claims]
    check("two_processes_only_one_claim", sorted(r["epoch"] or 0 for r in winners) == [0, 2])
    owner = "B" if winners[0]["epoch"] else "C"
    new = FencedSaver(thread, owner, 2)
    release.touch()
    check("late_old_checkpoint_rejected", (await finish(late)) == {"rejected": True})
    await rejected("late_pending_writes_rejected", saver.aput_writes(saved_config, [("values", ["poison"])], "old"))
    current = await new.aget_tuple(config(thread))
    check("late_writer_did_not_advance_head", current.config == saved_config and current.pending_writes == [])
    check("history_only_valid_checkpoint", len([item async for item in new.alist(config(thread))]) == 1)
    await rejected("wrong_owner_same_epoch_rejected", FencedSaver(thread, "wrong", 2).aget_tuple(config(thread)))
    bob = scoped_thread("bob", "race")
    await claim(bob, "bob-worker")
    bob_saver = FencedSaver(bob, "bob-worker", 1)
    check("same_local_id_other_user_empty", await bob_saver.aget_tuple(config(bob)) is None)
    await rejected("cross_user_explicit_id_rejected", bob_saver.aget_tuple(saved_config), PermissionError)
    await rejected("unscoped_history_rejected", bob_saver.call("alist", None), PermissionError)

    # 持有行锁后租约到期；竞争进程应等待事务结束，旧写入必须回滚。
    hold_thread = scoped_thread("alice", "hold")
    await claim(hold_thread, "A", 8)
    ready, release = WORK / "hold.ready", WORK / "hold.release"
    held = spawn(mode="hold", thread=hold_thread, owner="A", epoch=1, ready=str(ready), release=str(release))
    await wait_file(ready)
    await wait_database("SELECT expires_at<=clock_timestamp() AS ready FROM spike_leases WHERE thread_id=%s", (hold_thread,))
    contender = spawn(mode="claim", thread=hold_thread, owner="B")
    await wait_database("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE application_name='b04-claim-B' AND wait_event_type='Lock') AS ready")
    check("takeover_waits_for_locked_transaction", contender[0].poll() is None)
    release.touch()
    check("expiry_between_check_and_write_rolls_back", await finish(held) == {"rejected": True})
    check("takeover_after_rollback", await finish(contender) == {"epoch": 2})
    check("rolled_back_checkpoint_and_blobs_absent", await FencedSaver(hold_thread, "B", 2).aget_tuple(config(hold_thread)) is None)

    # 真正终止进程：分别覆盖 checkpoint+blob 以及 pending writes 未提交窗口。
    for mode in ("crash", "crash_writes"):
        thread = scoped_thread("alice", mode)
        await claim(thread, "A")
        saver = FencedSaver(thread, "A", 1)
        original = await put(saver, thread)
        ready, release = WORK / (mode + ".ready"), WORK / (mode + ".release")
        proc = spawn(mode=mode, thread=thread, owner="A", epoch=1, ready=str(ready), release=str(release), checkpoint_id=original["configurable"]["checkpoint_id"])
        await wait_file(ready)
        proc[0].kill()
        await asyncio.to_thread(proc[0].wait, 10)
        await expire(thread)
        await claim(thread, "B")
        recovered = await FencedSaver(thread, "B", 2).aget_tuple(config(thread))
        check(mode + "_process_kill_rollback", recovered.config == original and recovered.pending_writes == [] and recovered.checkpoint["channel_values"]["values"] == ["safe"])
        async with await connect() as conn:
            row = await (await conn.execute("SELECT count(*) AS n FROM checkpoint_blobs WHERE thread_id=%s AND version='2'", (thread,))).fetchone()
            check(mode + "_no_uncommitted_blob", row["n"] == 0)

    thread = scoped_thread("alice", "expired")
    await claim(thread, "A")
    expired = FencedSaver(thread, "A", 1)
    cp_config = await put(expired, thread)
    await expire(thread)
    await rejected("expired_without_takeover_checkpoint_rejected", put(expired, thread))
    await rejected("expired_without_takeover_writes_rejected", expired.aput_writes(cp_config, [("values", ["bad"])], "expired"))

    # 对照组确认普通 saver 不检查业务租约，不将原型能力归给库本身。
    async with await connect() as conn:
        raw = AsyncPostgresSaver(conn)
        await raw.aput_writes(cp_config, [("values", ["unfenced"])], "raw")
        observed = await raw.aget_tuple(cp_config)
        check("raw_saver_accepts_expired_worker_control", any(w[2] == ["unfenced"] for w in observed.pending_writes))

    check("legacy_snapshot_isolated", recovery_backend("legacy", 1, "json") == "legacy-isolated")
    check("new_snapshot_routed", recovery_backend("b04", 1, "postgres") == "postgres")
    try:
        recovery_backend("b04", 999, "json")
    except ValueError:
        check("unknown_snapshot_rejected", True)
    else:
        check("unknown_snapshot_rejected", False)
    async with await connect() as conn:
        postgres = (await (await conn.execute("SHOW server_version")).fetchone())["server_version"]
    result = {"python": platform.python_version(), "postgres": postgres, "versions": {p: md.version(p) for p in ("langgraph", "langgraph-checkpoint", "langgraph-checkpoint-postgres", "psycopg", "langchain-core", "langgraph-prebuilt", "langgraph-sdk")}, "cases": CASES}
    (ROOT / ".runtime" / "results.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"{len(CASES)} cases passed", flush=True)


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(main())
    finally:
        for process in PROCESSES:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)

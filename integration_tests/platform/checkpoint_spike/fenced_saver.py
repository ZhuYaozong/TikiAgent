"""验证原型：公开 saver API 与租约校验共用一个连接、一个短事务。"""
import json

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from lease_store import connect


class LeaseLost(RuntimeError):
    pass


def scoped_thread(principal, local_id):
    # 身份必须来自可信调用方；此原型不承担 JWT 校验。
    return json.dumps([principal, local_id], ensure_ascii=True, separators=(",", ":"))


def recovery_backend(runtime_version, state_version, backend):
    if (runtime_version, state_version, backend) == ("legacy", 1, "json"):
        return "legacy-isolated"
    if (runtime_version, state_version, backend) == ("b04", 1, "postgres"):
        return "postgres"
    raise ValueError("未知恢复版本，不自动转换旧快照")


class FencedSaver(AsyncPostgresSaver):
    def __init__(self, thread, owner, epoch):
        # 父类负责序列化器及版本生成；所有数据库入口都在下方显式覆盖。
        super().__init__(None)
        self.thread, self.owner, self.epoch = thread, owner, epoch
        self.after_lock = None
        self.before_commit = None
        self.writes_committed = None

    def check_scope(self, config):
        if config is None or config.get("configurable", {}).get("thread_id") != self.thread:
            raise PermissionError("不允许跨主体或无范围读取")

    async def call(self, method, config, *args, **kwargs):
        self.check_scope(config)
        async with await connect() as conn:
            async with conn.transaction():
                row = await (await conn.execute(
                    "SELECT owner_id,epoch,expires_at>clock_timestamp() AS live "
                    "FROM spike_leases WHERE thread_id=%s FOR UPDATE", (self.thread,),
                )).fetchone()
                if not row or (row["owner_id"], row["epoch"], row["live"]) != (self.owner, self.epoch, True):
                    raise LeaseLost("租约过期或已被接管")
                if self.after_lock:
                    await self.after_lock()
                saver = AsyncPostgresSaver(conn, serde=self.serde)
                if method == "alist":
                    result = [item async for item in saver.alist(config, *args, **kwargs)]
                else:
                    result = await getattr(saver, method)(config, *args, **kwargs)
                if self.before_commit:
                    await self.before_commit()
                # 校验期间到期也回滚，禁止先校验、慢写入后直接提交。
                live = await (await conn.execute(
                    "SELECT expires_at>clock_timestamp() AS live FROM spike_leases WHERE thread_id=%s",
                    (self.thread,),
                )).fetchone()
                if not live["live"]:
                    raise LeaseLost("事务结束前租约已到期")
                return result

    async def aput(self, config, checkpoint, metadata, new_versions):
        return await self.call("aput", config, checkpoint, metadata, new_versions)

    async def aput_writes(self, config, writes, task_id, task_path=""):
        result = await self.call("aput_writes", config, writes, task_id, task_path)
        if self.writes_committed:
            self.writes_committed(writes)
        return result

    async def aget_tuple(self, config):
        return await self.call("aget_tuple", config)

    async def alist(self, config, *, filter=None, before=None, limit=None):
        if before is not None:
            self.check_scope(before)
        for item in await self.call("alist", config, filter=filter, before=before, limit=limit):
            yield item

    async def aget_delta_channel_history(self, *, config, channels):
        # 本刀仅验证普通 channel；不让未覆盖路径绕过租约校验。
        raise NotImplementedError("B04 未开放 delta channel")

    async def adelete_thread(self, thread_id):
        raise NotImplementedError("运行 worker 不提供删除入口")

    async def setup(self):
        raise NotImplementedError("迁移仅由独立初始化步骤执行")

"""仅连接 B04 专用临时库，不读取应用 DATABASE_URL。"""
import os
from urllib.parse import urlparse

import psycopg
from psycopg.rows import dict_row

DEFAULT_DSN = "postgresql://b04:disposable-b04@127.0.0.1:38445/b04_checkpoint"


def validated_dsn():
    value = os.environ.get("B04_TEST_DATABASE_URL", DEFAULT_DSN)
    url = urlparse(value)
    if (url.scheme != "postgresql" or url.hostname != "127.0.0.1"
            or url.port != 38445 or url.path != "/b04_checkpoint"
            or url.username != "b04" or url.password != "disposable-b04" or url.query):
        raise ValueError("只允许 B04 固定测试实例")
    return value


async def connect():
    return await psycopg.AsyncConnection.connect(
        validated_dsn(), autocommit=True, row_factory=dict_row,
        options="-c statement_timeout=10000 -c lock_timeout=5000",
    )


async def claim(thread, owner, seconds=60):
    """短事务领取；数据库时间判断到期，接管时原子递增 epoch。"""
    async with await connect() as conn:
        await conn.execute("SELECT set_config('application_name', %s, false)", ("b04-claim-" + owner,))
        row = await (await conn.execute(
            "INSERT INTO spike_leases VALUES (%s,%s,1,clock_timestamp()+%s*interval '1 second') "
            "ON CONFLICT (thread_id) DO UPDATE SET owner_id=EXCLUDED.owner_id, "
            "epoch=spike_leases.epoch+1, expires_at=EXCLUDED.expires_at "
            "WHERE spike_leases.expires_at <= clock_timestamp() RETURNING epoch",
            (thread, owner, seconds),
        )).fetchone()
        return row["epoch"] if row else None

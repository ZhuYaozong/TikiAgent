# B04 检查点与租约隔离验证

独立验证工程，不导入两端业务应用，不读取应用 `.env` 或 `DATABASE_URL`。
需要 Docker Compose v2 和 Python 3.13（TiKi）/3.12（JobPilot）。

在本目录执行，选择一种 profile，两个 profile 必须顺序运行（共享专用端口）：

```powershell
uv venv .venv-tiki --python 3.13
uv pip sync --python .venv-tiki/Scripts/python.exe --require-hashes requirements-tiki.lock
& .venv-tiki/Scripts/python.exe run.py

uv venv .venv-jobpilot --python 3.12
uv pip sync --python .venv-jobpilot/Scripts/python.exe --require-hashes requirements-jobpilot.lock
& .venv-jobpilot/Scripts/python.exe run.py
```

Linux 对应使用虚拟环境的 `bin/python`。CI 在独立 runner 中分别运行两套配置。
也可以使用 `python -m venv` 和 `python -m pip install --require-hashes -r requirements-<profile>.lock`。

`run.py` 检查专用 Compose 项目和 38445 端口未占用，创建 tmpfs PostgreSQL，运行验证后清理自身容器。
只允许 `127.0.0.1:38445/b04_checkpoint` 的固定测试连接；测试密码 `disposable-b04` 仅用于临时本地容器。
已有 B04 容器会导致启动拒绝，不会接管或清理已有实例。
`.runtime/results.json` 是当前成功运行结果；旧结果会归档，失败不能沿用旧结果冒充通过。
中途终止整个 runner 时，可确认项目身份后执行 `docker compose -f ../../../deploy/platform/spikes/checkpoint.compose.yaml down`。

验证内容：异步图、interrupt、新进程恢复、pending writes、双 worker 争抢、晚到写入、行锁等待、事务内到期回滚、真正 kill 进程、跨用户引用与版本拒绝。
并行失败图通过提交完成事件控制顺序，租约竞态通过数据库锁等待状态确认，不依靠短暂停顿推测顺序。

`FencedSaver` 是有边界的可行性原型：只接入普通 channel 与本刀的图结构；不提供任务队列、续租服务、完整生产权限、旧数据迁移或外部副作用去重。
生产接入前需要 T03/T04/J05 补齐服务端身份绑定、生命周期和连接池策略。

依赖输入固定两端当前 LangGraph、checkpoint、core、prebuilt、sdk、序列化依赖的版本，新增 saver/driver 单独锁定。
其他传递依赖以本工程完整哈希锁为准，不宣称等于两个生产应用的完整依赖环境。
更新锁文件：`uv pip compile requirements-<profile>.txt --python-version <3.13或3.12> --generate-hashes -o requirements-<profile>.lock`。

实测结论见 [checkpoint-spike.md](../../../docs/platform/checkpoint-spike.md)。

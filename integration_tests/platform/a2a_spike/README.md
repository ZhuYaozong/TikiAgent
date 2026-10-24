# B05 A2A 互操作验证

独立测试工程，使用官方 `a2a-sdk==1.2.3`，A2A 1.0 JSON-RPC + SSE。监听 `127.0.0.1:38446`，无真实业务数据、模型、数据库或 OIDC 连接。

在本目录执行（先安装 Python 3.12、3.13 和 uv）：

```powershell
uv venv .venv-client --python 3.13
uv venv .venv-server --python 3.12
uv pip sync --python .venv-client/Scripts/python.exe --require-hashes requirements.lock
uv pip sync --python .venv-server/Scripts/python.exe --require-hashes requirements.lock
$env:B05_SERVER_PYTHON = Join-Path (Get-Location) '.venv-server/Scripts/python.exe'
& .venv-client/Scripts/python.exe run.py
```

反向组合互换客户端和服务端解释器。Linux 使用对应 `bin/python`，也可用标准 `venv` 和 `pip install --require-hashes -r requirements.lock`。

运行器拒绝占用端口，只终止自己创建的进程，清理跨版本 `PYTHONHOME/PYTHONPATH` 继承；每次运行将旧结果归档，成功后写 `.runtime/results.json`。日志、虚拟环境和中间结果不提交，CI 只上传脱敏结果。

`disposable-b05-alice/bob` 是固定的测试 token，只在本地 fixture 有效；不具备 JWT 验签、有效期、撤销、授权续期能力。服务端内存状态在进程退出后丢失；后台 asyncio task 不等于持久 worker。不要把本工程部署为业务服务。

`instruction` 的 complete/input/auth/slow/uncancelable 是测试执行器场景选择，不是正式技能正文。认证等待中的 reference 仅测试类型与路由，不执行真实授权验证。

完整 B02 命令作为 SendMessage.metadata 扩展传递；CancelTask 通过协商的 X-Tiki-Command 请求头传递。所有正文都必须通过冻结 schema，身份来自测试认证表而不是模型输入。标准 SDK 负责 wire 编解码，handler 负责应用约束。

冻结绑定见 [a2a-binding-v1.md](../../../docs/contracts/a2a-binding-v1.md)，结果见 [a2a-spike.md](../../../docs/platform/a2a-spike.md)。

依赖锁生成命令：`uv pip compile requirements.txt --python-version 3.12 --generate-hashes -o requirements.lock`。这是独立协议环境，未升级两端生产依赖；嵌入 JobPilot FastAPI 时必须重新核对 Starlette/Pydantic 等完整依赖组合。

# B01 实施与验收记录

日期：2026-10-23。状态：B01 完成；其余工作包尚未实施。本次未提交 commit、推送或创建 PR。

## 代码与环境基线

| 项目 | HEAD | 实施前已有改动 |
|---|---|---|
| TiKiAgent | d412435fc173e14c4e05c86e2c8f217c9e2bc699 | 未跟踪的 .start |
| JobPilot | 67c7d4fed88a226a3cffb80421b17a938725cac4 | .start；services/rag_models/run.ps1 |

以上用户已有改动均保留，没有重置或覆盖。两项目本地虚拟环境实际均为 Python 3.13.12；声明分别为 TiKi ≥3.13、JobPilot ≥3.12。Docker Server 28.4.0 可用。使用本机已有 pgvector/pgvector:pg17 镜像，没有安装/升级依赖。

依赖锁文件未改动：

- JobPilot backend/uv.lock SHA256：829818ACEC34D3608D2A9D33938F0170C51EF23F190920BD28E1F30DC9C8C9E5。
- TiKi uv.lock SHA256：430F1C2191CAB0C25E0C7E38D74FDC9E434ACDA48C72138018050BB15809FAD5。

## 实际改动

JobPilot 修改：
- backend/tests/conftest.py：导入应用/数据库引擎前验证测试目标；首次加载 Settings 时避开仓库 .env；每测试临时文件目录与网络限制。
- .gitignore：允许提交 backend/.env.test.example。
- README.md：更新旧测试命令说明、增加隔离环境启动/迁移/测试/清理方法。
- .github/workflows/ci.yml：使用同一测试 Compose 和受护栏保护的迁移入口，always 清理。

JobPilot 新增：
- backend/testing_environment.py：测试 URL 校验、测试配置覆盖、独立迁移入口。
- backend/tests/test_test_environment_guard.py：启动顺序、配置拒绝、网络/文件隔离与 Windows socketpair 兼容测试。
- backend/.env.test.example：不自动加载的专用配置样例。
- backend/compose.test.yaml：本机 35432 端口的临时 PostgreSQL/pgvector。
- backend/tests/bootstrap_test_db.sql：非超级用户角色、专用数据库、扩展及权限设置。

TiKi 新增：docs/platform/baseline.md（本记录）。没有修改 TiKi Python 代码。

没有修改生产 Settings、业务 Service、Agent Runtime、依赖声明或锁文件。新增辅助模块、容器初始化与 CI 变更都是使 B01 护栏可运行、可复现所需的测试基础设施。

## 隔离机制与验收

1. TEST_DATABASE_URL 必须显式提供，只接受 127.0.0.1:35432、jobpilot_test 数据库和同名角色；拒绝 URL options、其他主机/端口/数据库/管理员角色。
2. DATABASE_URL 不作为回退。非法配置探针在导入 app 或 sqlalchemy 前拒绝；错误信息不回显口令。
3. 首次配置加载发生在空临时目录；测试进程覆盖模型/MCP/Redis/认证测试配置，避免读取仓库 .env 或使用继承的模型密钥。
4. 每个测试的相对文件输出进入 pytest 临时目录，真实 TCP 连接仅允许测试库。
5. Windows 标准库 socketpair 内部调用有严格限定的直接调用例外，使 asyncio 能建立内部通信；没有放开任意本机端口。
6. 临时数据库角色 rolsuper/rolcreatedb/rolcreaterole/rolreplication 均为 false；对 postgres/template1 的 CONNECT 均为 false；对 jobpilot_test 为 true。
7. 现有数据库迁移只应用在本次新建的测试实例。现有业务实例及其数据未连接、未迁移。
8. 测试容器使用 tmpfs，完成后移除本次实例和专用网络。

护栏属于测试误连防护，不是对不可信测试代码的操作系统级沙箱；测试自身能主动修改配置或绕过 monkeypatch，因此仍须审查新增测试。

## 验证记录

| 验证 | 结果 |
|---|---|
| 独立 unittest 护栏测试 | 7 项通过；无需数据库，验证无 app/engine 导入的拒绝路径 |
| 初始护栏与用户隔离小范围 pytest | 17 项、16 个子用例通过 |
| TiKi test_checkpoint.py + test_permission_policy.py | 15 项离线测试通过 |
| JobPilot 后端全部 pytest（隔离库，真实外网/模型连接受阻） | 最终 375 项、16 个子用例通过，52.77 秒 |
| 现有 Alembic 迁移应用到全新测试库 | 成功到 e8f3a1c9d204 |
| 专用角色权限 SQL 核验 | 通过 |
| docker compose config --quiet | 通过 |
| 新增/修改 Python 文件语法解析 | 通过 |
| git diff --check | 最终检查见本轮执行结果；无本刀遗留格式问题 |
| GitHub Actions 远端执行 | 未触发，待后续获授权的提交/推送后验证 |

扩大回归的原因是本刀更改了共享测试 fixture 和 CI。先通过配置护栏与小范围测试，再检查测试数据库/文件调用入口后执行完整回归。

中间发现并修复：Windows asyncio 内部 socketpair 被最初网络限制误拦。增加窄范围例外及回归测试后，全量回归通过。该错误未遗留。

## 限制与后续

- 375 项通过证明当前行为未被测试基础设施改造破坏，不代表既有认证/审批问题已经修复。已有“开发用户注册升级”等测试仍验证旧行为；J01 后须更新预期。
- 本地完整验证使用 Windows/Python 3.13；CI 的 Ubuntu/Python 3.12 路径尚未远端验证。
- 专用端口/数据库/角色是有意固定的测试允许目标；如需要并行 CI worker 或其他端口，应另行修改允许策略及测试，而不是复用业务连接。
- 模型、MCP 和公网真实服务没有执行集成验证；使用既有替身及网络护栏。
- B02“冻结跨服务契约与任务所有权”为下一工作包，尚未开始。仍按逐刀方案执行，不把本次 B01 完成当作其余任务的实施结果。

# B04：LangGraph 检查点与租约隔离实测

日期：2026-10-24。TiKi 基线：`d942a1200ee60634de8a6c286fb5fd264352794f`（B03 已合并）。
本刀是独立兼容性验证，不是 T04/J05 的生产持久化接入。

## 结论

选择 **事务 saver 适配路径**：租约行锁、owner/epoch 校验、原生 saver 写入、到期复查均在同一个 PostgreSQL 连接的显式短事务内。两端核心依赖组合各 36 项验证通过，共 72 项。

此组合无需修改 saver 私有 `_cursor` 或复制库内 SQL。原生 `AsyncPostgresSaver` 接收调用方连接，公开 `aput` / `aput_writes` 的 pipeline 在外层事务中执行；真正 kill worker 后 checkpoint/blob/pending writes 回滚的场景验证了事务边界，而非仅凭源码推断。

不另加有效 checkpoint head 表。本刀封闭的写入路径中，过期 worker 的写入无法提交，因此不会发布更晚的非法 checkpoint。生产中若引入其他写入入口、历史分叉、外部 graph update 或 delta channel，需要重新验证恢复 head 的选择规则；不能直接套用本结论。

原生 saver 的对照测试明确显示：即使业务租约已过期，直接调用它仍能写入 pending writes。后续必须统一使用受控适配器；普通 `compile(checkpointer=...)` 不等于租约隔离。

## 验证版本

| 项目 | TiKi profile | JobPilot profile |
|---|---|---|
| Python | 3.13.12 | 3.12.7 |
| LangGraph | 1.2.11 | 1.2.1 |
| langgraph-checkpoint | 4.2.0 | 4.1.1 |
| langchain-core | 1.6.0 | 1.4.0 |
| langgraph-prebuilt | 1.1.0 | 1.1.0 |
| langgraph-sdk | 0.4.3 | 0.3.15 |
| langgraph-checkpoint-postgres | 3.1.2 | 3.1.2 |
| psycopg | 3.3.6 | 3.3.6 |
| PostgreSQL | 16.13 | 16.13 |

核心版本来自两个仓库锁文件，并核对 JobPilot `origin/main`。saver 与 driver 是新增验证依赖；其他传递依赖由独立 `requirements-*.lock` 固定并校验哈希。本刀不宣称运行了两个应用的完整生产依赖环境或业务回归。镜像固定为 `postgres:16@sha256:78df81b1442dcc764c1104154da7162635e40cfffe67579c42a1c1b96dfc209c`。

## 关键证据

完整结果见 [脱敏结果](checkpoint-spike-results.json)，复现见 [验证工程](../../integration_tests/platform/checkpoint_spike/README.md)。

| 场景 | 实际观察 |
|---|---|
| interrupt 后退出进程、接管再恢复 | 最终 approved；等待节点开头执行两次 |
| 同 superstep 成功节点与失败节点 | good 的 pending write 已入库；新进程恢复时 good 计数 1、flaky 计数 2 |
| 两个独立进程同时接管 | 只有一个拿到 epoch 2 |
| A 暂停、B 接管、A 再写 | A 的 checkpoint 拒绝；旧 epoch pending writes 也拒绝；有效 head 未改变 |
| A 持有租约行锁后过期、B 接管 | 数据库实际显示 B 等待 Lock；A 写入后复查过期并回滚，B 随后接管 |
| checkpoint/blob 已执行 SQL、提交前 kill | 新进程仅能读取先前已提交快照，新 blob 不存在 |
| pending writes 已执行 SQL、提交前 kill | 新进程读到的 pending writes 不含被终止进程的内容 |
| 租约到期但无人接管 | checkpoint 和 pending writes 均拒绝 |
| 普通 saver 对照 | 接受过期 worker 的 pending writes，证实库本身不提供业务 fencing |
| 用户范围 | 相同局部 thread ID 不串数据；跨用户显式 checkpoint 引用和无范围历史读取被拒绝 |
| 快照版本 | legacy/json 与 b04/postgres 显式分流，未知组合拒绝 |

首次试验发现两个测试仪表问题，已修正后重新完整运行：Windows 并行节点追加计数文件需要锁；失败节点必须等待成功兄弟的 pending writes 真正提交，才能确定性测试成功写入复用。未将首次失败包装成库缺陷。

## 实现边界与后续落点

- `lease_store.claim` 使用单条 UPSERT 原子领取/递增 epoch；所有时间判断采用数据库 `clock_timestamp()`。
- `FencedSaver.call` 在同连接事务中 `SELECT ... FOR UPDATE`，校验 owner、epoch 和到期时间；随后调用原生 saver，再复查到期。读取也校验当前执行租约。
- 行锁将写入与接管串行化。最后一次到期检查与实际 COMMIT 存在正常执行间隔；本刀证明接管无法穿过该事务，并不承诺物理提交时间严格早于租约截止时刻。
- 测试每次打开一个连接以明确事务归属。T04/J05 可改连接池，但不能让 saver 在事务之外另取连接。模型/网络调用不能放在持有租约行锁的事务中。
- pending writes 与 checkpoint 分别是短事务；已提交 pending writes 供同一逻辑任务恢复。它们与领域业务提交不构成同一原子事务，业务副作用仍需要 J03 操作账本。
- 当前支持普通 channel，delta channel 和删除入口明确关闭；嵌套子图、流式取消、生产续租、长图性能、序列化升级和完整业务图均未验收。
- `scoped_thread` 仅演示可信主体与局部 ID 组合。JWT 验证、issuer+subject 绑定及服务端 owner 校验属于 T01/J01，不能把模型提供的 principal 直接传入。
- 旧 `JsonCheckpointStore` 和 `WorkflowResumeSnapshot` 不自动迁移。后续通过 runtime/state/backend 版本分流，旧任务排空或留在隔离旧路径；本刀只验证路由判定，不声称完成旧快照读取或迁移器。
- T03 负责持久任务、续租和 worker 生命周期；T04/J05 接入此事务边界并关闭绕过路径。在这些生产接入验收前，仍不开放实际业务任务的自动抢占。

本机两套验证均已完成，测试容器和网络已清理。CI 已配置两套矩阵；本地验收不等于远程 CI 通过，远程结果以对应提交的 GitHub Actions 为准。

参考：[官方 saver 源码](https://github.com/langchain-ai/langgraph/blob/main/libs/checkpoint-postgres/langgraph/checkpoint/postgres/aio.py)、[官方 checkpoint 文档](https://github.com/langchain-ai/docs/blob/main/src/oss/langgraph/checkpointers.mdx)。实际判断以本机安装的 3.1.2 源码和固定依赖实测为准。

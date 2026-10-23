# B02：冻结跨服务契约与任务所有权

日期：2026-10-23。状态：契约和双语言验证已完成，本刀尚未提交或推送；远程 CI 未运行。

## 交付

TiKiAgent `docs/contracts/` 新增 13 份 Draft 2020-12 JSON Schema、身份与任务所有权规范、状态转换表、64 个结构正反例和 40 个语义场景。`tools/contract-validation/` 提供独立 Python/Java 校验器，`.github/workflows/contracts.yml` 提供跨语言验证 CI，根 README 增加入口。

唯一根执行状态归 TiKi，JobPilot 仅维护领域子任务，平台保存受理/派发事实和只读投影。明确按来源任务比较 revision、同键异参冲突、相同内容的新操作使用新 ID、输入一次消费、精确版本审批、未知副作用核对及父子预算约束。

没有改动现有业务运行时或数据库；没有创建 Spring Boot 业务服务。JobPilot 后续以 TiKi 的契约提交/tag 为版本来源，不共享 ORM。

## 实际验证

- Python：13 份 Schema 元校验、共享定义一致性、104 个样例全部通过。
- Java：相同 13 份 Schema、104 个样例全部通过。
- 两端每条结论逐行一致；Maven 离线 `-o compile exec:java` 复验成功。
- 日期反例暴露 Python 可选格式依赖缺失会放行错误日期，已增加显式真实日期检查；两端均拒绝 2026-02-30。
- 覆盖跨用户事件、错误来源、旧 revision、重复命令、同键异参、重复输入、过期/变更审批、完成与取消竞争、未知副作用和预算超额。

环境：Python 3.13.12、jsonschema 4.26.0；Java 22.0.2，编译目标 release 17；Maven 3.9.0；networknt 1.5.6。本机未在 Java 17 上运行；新增 CI 配置使用 Temurin 17，尚未远程验证。

首次 Maven 下载遇到网络截断/TLS 中断。使用 Windows curl 从同一 Maven Central 下载固定依赖并核对仓库摘要后补齐缓存，随后构建及离线验证成功。未关闭证书检查，未修改全局 Git/Maven 配置。

复现命令见 `docs/contracts/README.md`。本地逐例日志保存在工作文档目录 `B02-staging/python.log` 和 `B02-staging/java.log`。

## 验收边界

本刀验证静态结构与六类语义参考决策，不证明生产鉴权、资源授权、数据库事务或并发效果已经实现。未实现 JCS 算法、grant 签名、事务 inbox/outbox、checkpoint fencing、A2A、技能正文及 SSE 服务；hash 样例是格式占位值，不是 JCS 算法向量。

B03 验证身份与受托授权，B04 验证检查点隔离，B05 冻结 A2A 映射；后续 T/J/P 工作包实现事务与服务端强制约束。B02 不自动开启下一刀。

## Git 状态

B01 已成功推送：JobPilot `283ab48`（`codex/jobpilot-mcp-datasets`）；TiKiAgent `491bdf3`（`codex/research-context-evidence`）。

B02 尚未提交/推送，没有创建 PR。TiKi 的用户未跟踪 `.start`，JobPilot 的用户改动 `.start`、`services/rag_models/run.ps1` 保持原状。

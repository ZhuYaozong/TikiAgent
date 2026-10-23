# TiKiAgent / JobPilot 跨服务契约 v1

B02 交付：静态 JSON Schema、任务所有权/状态表、共用正反例，以及独立 Python/Java 验证程序。现有 CLI、Agent、数据库和 JobPilot 业务入口尚未接入这些契约。

先读 [身份、所有权和事件语义](identity-and-events.md)，再使用 schema；schema 通过并不代表鉴权或跨字段业务检查通过。

| 文件 | 内容 |
|---|---|
| task-v1.schema.json | PlatformTask 与 RuntimeRun/DomainTask 快照 |
| identity-v1.schema.json | 已认证的内部用户与服务上下文 |
| task-submission-v1.schema.json | 公共提交载荷，无顶层用户身份 |
| delegation-v1.schema.json | 领域委派、版本化资源和预算 |
| command-v1.schema.json / command-receipt-v1.schema.json | 四类命令和持久接收回执 |
| input-request-v1.schema.json | 补充输入、审批、认证和核对等待 |
| agent-result-v1.schema.json | 领域结果、来源、工件、操作回执和用量 |
| action-proposal-v1.schema.json / approval-decision-v1.schema.json | 执行方提案与精确版本的用户决定 |
| event-v1.schema.json | 带权威快照的领域/根任务事件 |
| error-v1.schema.json / budget-v1.schema.json | 稳定错误分类和预算预留 |
| ownership-and-transitions.json | 唯一写入方及合法状态转移 |
| fixtures/schema-cases.json / fixtures/semantic-cases.json | 两种语言共同消费的黄金样例 |

## 运行验证

在仓库根目录，Python 3.11+：

```powershell
python -m venv tools/contract-validation/.venv
tools/contract-validation/.venv/Scripts/python -m pip install -r tools/contract-validation/requirements.txt
tools/contract-validation/.venv/Scripts/python tools/contract-validation/verify_python.py
```

Linux/macOS 使用该 venv 的 `bin/python`。仅安装测试依赖，不修改 Agent 运行依赖。

Java 17+、Maven 3.9+，在仓库根目录运行：

```powershell
mvn -B -q -f tools/contract-validation/java/pom.xml compile exec:java "-Dexec.args=docs/contracts"
```

Maven 首次构建需要下载固定版本校验库与构建插件，后续可加 `-o` 使用缓存；schema 解析不联网。Java 与 Python 均输出每例 `id=结论`，最后输出 PASS 统计。CI 独立比较两端输出，任一结果偏离预期或两端不同均失败。Java 这里只是协议消费样例，没有创建 Spring Boot 业务后端。

## 变更规则

本目录为单一来源；JobPilot 后续锁定 TiKi Git commit/tag 消费。共享协议与向量，不共享 ORM 或业务类。先更新版本协商和消费者，再发送新版本。任何字段修改都必须更新正反例，并重新运行两种语言验证。

本刀记录见 [B02 验收记录](../platform/B02-contracts.md)。A2A binding、OIDC 产品、生产 endpoint 和技能正文将在各自依赖任务中验证；不把本目录作为这些能力已经运行的证据。

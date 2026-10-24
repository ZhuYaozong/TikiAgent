# TiKi–JobPilot A2A binding v1

状态：B05 独立工程验证通过。此文定义双边应用绑定，生产兑现由 J04/J07/T05 完成；不代表已经存在生产 A2A 服务。

## 协议选择

- A2A 规范 1.0.0；HTTP `A2A-Version: 1.0`；AgentInterface.protocolVersion=`1.0`。
- 官方 Python SDK 1.2.3，JSONRPC binding，流式响应采用 SSE；不开放旧版 0.3 兼容入口、gRPC、REST binding 或 webhook。
- 方法为 SendMessage、SendStreamingMessage、GetTask、CancelTask、SubscribeToTask。订阅是当前任务状态及后续事件，不承诺历史事件游标重放。
- 必需扩展 URI：`https://contracts.tikiagent.invalid/a2a/binding/v1`。URI 是命名标识，不需要 DNS 解析；未来更换标识需双方显式升级。
- AgentCard 声明扩展 required=true，调用方通过 `A2A-Extensions` 协商。未知正文版本、缺失协商均拒绝。
- 端点来自受控目录。AgentCard 只能与目录核对，不能作为身份或地址授权证明。

## ID 与字段映射

| 字段 | 语义 |
|---|---|
| B02 target_task_id | TiKi 预分配的领域任务 ID，保持原契约 |
| B02 delegation.remote_task_id / result.remote_task_id | 领域任务 ID；不改解释为 A2A wire ID |
| A2A Task.id | 接收端生成的协议任务 ID，持久映射到领域任务 |
| A2A Task.metadata[扩展 URI].domain_task_id | 返回领域 ID，供消费端核对 |
| A2A Task.metadata[扩展 URI].delegation_id | 返回逻辑委派 ID |
| A2A Message.taskId | 新委派必须为空；响应输入时使用已返回的 A2A ID |
| A2A contextId | 服务端分配，不代表资源访问权；本刀不接收调用方选择的 contextId |
| Message.messageId / JSON-RPC id | 消息追踪/请求响应关联，不充当业务幂等键 |

J04/J07 必须原子建立 `(principal_id, domain_task_id) → a2a_task_id`，T05 保存两种 ID。不能把 B02 的预分配 ID 填进首次 Message.taskId。

## 提交和补充输入

SendMessage.metadata 只包含扩展 URI 键，其值为 `{version: "1.0", command: <完整 B02 command-v1>}`。
Message.role=ROLE_USER；验证工程 Message.parts 为固定文本 `B02 command envelope`，执行参数只从经过校验的 command.payload 获取，不从自由文本推断。

新委派使用 kind=delegate；续接使用 kind=respond，校验 principal、platform_task_id、target_task_id、input_request_id、expected_revision、response_type。凭据走 Authorization，不进入消息或工件。

结果通过 Artifact.parts.data 携带完整 B02 agent-result-v1 信封，Artifact.extensions 声明本绑定。A2A completed 只说明领域任务完成；根任务终态和最终验证权仍归 TiKi。

## 取消扩展

A2A 1.0 CancelTaskRequest 没有 metadata。选择保留标准 CancelTask 方法，并通过扩展 HTTP 头 `X-Tiki-Command` 传递 URL-safe Base64 编码的 UTF-8 JSON B02 cancel 命令（不是加密）。

接收端最大允许 8192 字符；缺少、非法编码、身份/任务不匹配、旧 revision 或 hash 错误均拒绝。生产网关需保留该头，并禁止将其正文记入普通访问日志。标准协议客户端若未实现本扩展，不能调用此服务的受控取消入口。

同一已接受取消命令可重复查询式返回；新的取消命令作用于已完成任务时返回 TaskNotCancelable。取消请求不是成功终态证明；生产若存在副作用未知，必须保持核对状态，不能直接置 canceled。验证工程无外部副作用，才允许立即取消。

## 幂等承诺

- 与 B02 一致，唯一键为 `(principal_id, actor, kind, idempotency_key)`，作用域为接收服务。
- 服务端重新计算 RFC 8785 JCS + SHA-256；规范化输入域完全沿用 B02，不包含 command_id、sent_at 等非语义字段。
- 同键同 hash 返回同一任务及当前状态；不同 hash 返回应用码 IDEMPOTENCY_CONFLICT。更换 command_id 不能绕过去重。
- 同一领域 ID 换幂等键不能创建第二个协议任务。新的合法相同操作需新的领域任务 ID 与幂等键。
- 已接受响应命令的重复投递优先返回原任务，即使任务已终态；新的响应命令不能重开终态任务。
- 生产去重记录和 ID 映射在任务非终态期间不得删除，终态后至少保留 30 天，且不得早于命令重投截止时间；存在未决副作用则持续保留。到期后需保留不可重执行的 ID 墓碑或明确拒绝过期命令，不得静默新建。
- 本刀仅用单进程锁和内存记录验证协议行为，进程生命周期内不清除；没有验证上述生产保留期限、数据库唯一约束或重启去重。

## 状态与错误

| 应用状态/情形 | A2A 表达 |
|---|---|
| 接收/执行 | submitted / working |
| 需要业务信息或审批 | input-required；应用 wait_kind 区分 clarification/approval |
| 需要重新认证 | auth-required；wait_kind=authentication |
| 完成/失败/取消/拒绝 | completed / failed / canceled / rejected |
| 终态新输入 | InvalidParamsError，应用说明 TERMINAL_TASK |
| 不存在或无权访问任务 | TaskNotFoundError (-32001)，不泄露所有权 |
| 已完成或不能取消 | TaskNotCancelableError (-32002) |
| 缺少扩展协商 | ExtensionSupportRequiredError (-32008) |
| 字段/幂等冲突/旧 revision | InvalidParamsError (-32602)，应用说明区分具体原因 |
| 缺少/无效认证 | HTTP 401，进入协议 handler 前拒绝 |

应用错误说明如 IDEMPOTENCY_CONFLICT、STALE_REVISION 是本绑定约定，不是 A2A 标准错误名。当前 probe 通过 SDK 标准异常类型及说明传递；完整 B02 error 信封映射和业务错误分类由 J07/T05 接入。

流式事件不携带 Java 平台事件的全部语义。消费者断流后先 GetTask，必要时 SubscribeToTask；不得依据断流自行宣布失败、取消或重新提交。终态订阅在本绑定中明确拒绝，最终结果通过 GetTask 获取。

## 验证边界

已验证 read 技能 fixture；未验证真实简历正文、审批授权、effect_unknown、资源版本竞争、预算执行、JWT、持久队列、生产 FastAPI 依赖组合或 SDK 默认任务 handler。应用 handler 实现了这些协议约束，不应将其能力归因于 SDK 本身。

参考：[官方 1.0 规范](https://a2a-protocol.org/v1.0.0/specification/)、[官方 Python SDK](https://github.com/a2aproject/a2a-python)。完整实测见 [B05 报告](../platform/a2a-spike.md)。

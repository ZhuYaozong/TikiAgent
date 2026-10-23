# v1 身份、任务所有权与跨服务语义

状态：B02 应用契约基线。本文的 MUST/必须是后续服务实现的验收条件；静态 schema 和参考校验器不代表生产端已经实现这些条件。A2A 的消息/状态/认证映射由 B05 独立验证后冻结，不把本目录的私有 JSON 直接称为 A2A。

## 1. 信任边界与标识

`issuer + subject` 唯一映射稳定 `principal_id`。`actor` 是当前调用服务，`audience` 是接收服务，两者不能替代用户主体。`identity-v1` 是认证适配器产生的内部上下文，不是浏览器可以声明的身份凭证；端点必须先验证令牌签名、issuer、audience、有效期、用户状态及 scope，再将上下文与请求字段比对。令牌交换、续期、撤销的实际方案由 B03 验证。

公共提交使用 `task-submission-v1`，不接受顶层 principal_id。所选资源中的 principal_id 只是客户端声明，必须与认证主体及数据库资源所有者三方一致；不能据此授权、改变主体或通过内部 endpoint 绕过认证。后续前端可以从服务返回的资源描述原样提交。任何 ID、hash、工件引用或 grant ID 都不是访问能力；跨用户查询可统一返回 NOT_FOUND，防止枚举。

ID 是服务器生成并持久化的不可猜测不透明字符串，schema 只限定可互操作的字符和长度，不替代随机性。操作不能采用模型临时 tool_call_id。浏览器可生成 submission_id 作为其请求幂等标识，但不能指定内部 task/run/thread/operation 身份。服务端将 thread_id 绑定 principal/run，不对外暴露任意 checkpoint 访问。

| 记录 | 唯一事实写入者 | 关系与限制 |
|---|---|---|
| PlatformTask | platform | 受理和派发事实；projection 是 TiKi 根任务的只读快照 |
| RuntimeRun | tiki | 首期每个 PlatformTask 恰好一个 run；task_id = run_id；唯一根执行状态 |
| DomainTask | jobpilot | 每个逻辑 delegation 对应一个当前 remote_task_id；只决定子任务状态 |
| delegation | tiki | principal/platform_task/run/skill/resource/budget 绑定；重投仍定位同一个领域任务 |
| operation | 实际执行方 | 持久化 operation_id 后执行；副作用事实写入操作账本 |
| approval | platform 记录决定，执行方记录消费 | 不承诺跨库事务；平台状态是消费结果的投影 |

同一任务的恢复只增加 attempt_no，不能创建新的根任务或重置预算。终态不可重开；显式重试新建任务并绑定 retry_of，经过重新授权。PlatformTask.projection 必须与外层 principal、task_id/platform_task_id、run_id 一致。领域资源、结果、工件、输入请求、提案中的主体和整条任务链必须一致。这些跨字段/跨库条件必须由服务层再次检查。

## 2. 状态与并发

合法状态转换以 [ownership-and-transitions.json](ownership-and-transitions.json) 为准。phase 表示执行阶段，不是另一套任务状态。非 WAITING_INPUT 不携带 wait_kind/input_request_id；等待状态必须同时携带两者。`cancel_requested_at` 只记录取消意图。

修改状态必须检查所属服务及 expected_revision，在本地事务中原子 CAS 更新并递增 revision。终态不可变；完成和取消竞争时，先持久化成功的权威终态保留，另一命令返回冲突或 ALREADY_TERMINAL。不得以父任务取消覆盖已经完成的子任务。

`effect_status=unknown` 时保持 RUNNING/reconciling，或 WAITING_INPUT/reconciliation；禁止直接终结。deadline 后停止创建业务操作，允许查询核对。已确认副作用取消后仍须保留回执，补偿是另一项经授权的操作。传输超时不能直接判定业务失败或安全重试。

COMPLETED 必须有 result_id、可交付内容与验收状态。partial 必须 accepted_partial 并提供非空 limitations；完整 ready 对应 accepted。根验收由 TiKi 完成，不能把 DomainTask.COMPLETED 直接映射为根完成。REJECTED 仅用于 QUEUED 的受理拒绝。单项审批 reject 后可以重新规划或 FAILED，不自动变成 REJECTED/CANCELED。

## 3. 命令、幂等与输入

命令种类：submit_run、delegate、respond、cancel，payload 必须匹配对应 schema。命令接收回执的 accepted 表示已持久化接收，不表示业务完成；duplicate 返回原始 task_id 和可查询回执，不能触发第二次执行。target_task_id 对 submit_run 是平台预分配的 run_id；对 delegate 是 TiKi 预分配的领域 task_id；对 respond/cancel 是已存在的目标。预分配不代表有权访问，接收端必须校验整条绑定并原子创建。

幂等唯一键为 `(principal_id, actor, kind, idempotency_key)`，在各接收服务内生效。同键同 hash 返回原记录；同键异 hash 返回 IDEMPOTENCY_CONFLICT。command_id 用于审计，不能通过换 command_id 绕开幂等键。接收者必须自行计算 hash，不能相信请求提供的 hash。用户两次明确要求相同操作时分配两个 operation_id/幂等键；参数 hash 不是操作身份。

命令规范化对象固定为 `{principal_id, actor, audience, kind, platform_task_id, target_task_id, payload}`，键名和值取自已验证命令；不包含 sent_at、command_id、idempotency_key 或 payload_hash。采用 RFC 8785 JCS 的 UTF-8 字节再 SHA-256，格式 `sha256:` + 64 位小写 hex。不得用普通 sort_keys 序列化冒充完整 JCS。执行提案的 hash 则覆盖执行方持久化的 `{action_type, principal_id, executor, operation_id, resources, parameters}`；parameters 是版本化技能专用业务输入，不是自由模型对象。B02 只冻结算法/输入域，校验器的 hash 样例为占位值，不声称已验证 JCS 算法或哈希内容，生产算法兼容测试随 T02/J04/P07 接入。

接收端 inbox/幂等记录与任务创建同一事务；发送端命令 outbox 与提交记录同一事务。至少一次投递，不承诺任意外部服务 exactly-once。幂等记录不得早于该操作的所有重试、恢复、审批与可查询窗口删除。v1 规定任务终结后最少保留 30 天，且不得早于任何相关授权/恢复窗口结束；活跃及 effect unknown 记录不得回收。超窗历史请求拒绝，不能当新操作执行；后续存储实现保留拒绝重放的 tombstone 或等效索引。用户删除数据时可清除正文，去标识化审计/去重元数据的保留政策另行落实。

输入必须引用当前 input_request_id、expected_revision、response_id、response_type；value 为 text 时是回答，其他类型是已认证服务记录的决定/认证/人工核对引用，不接受 access token。输入一次消费并原子推进状态。同一已消费响应重投返回原结果（即使当前 revision 已推进）；内容冲突、错误请求、过期响应拒绝。认证恢复需要重新验证用户与 scope，不能靠一个任意引用解除等待。

## 4. 事件与投影

事件 source 只允许 tiki/jobpilot，必须由对应已认证来源发布。snapshot 是完整权威快照：source = owner、source_task_id = task_id、source_revision = revision，principal/platform_task/run 均须与已有映射一致。领域事件还须核对持久化 delegation→remote_task 映射，不能凭一条事件创建未经授权的关联。

revision 只在 `(source, source_task_id)` 范围内比较；seq 是同范围内的持久事件序号，不是全局顺序。每次对外可观察状态变化递增 revision；同一 revision 可以有不同 type 的附属事件，投影只应用一次完整 snapshot，但必须保留并处理未重复的附属事件。`source + event_id` 去重，事件日志、去重记录、投影更新同一事务。参考场景中的 IGNORE 只指忽略快照更新，**不等于丢弃尚未记录的事件**。乱序低 revision 不能覆盖新投影。

根投影只接收映射到该 RuntimeRun 的 TiKi 快照；JobPilot 快照仅更新子投影。根状态比较不能使用子任务 revision，即使它更大。订阅建立、重连、快照读取都需鉴权。持久事件不得含访问令牌、完整提示词、未裁剪简历正文或主机路径。

平台 SSE 使用自己的不透明事件 cursor，不能直接使用远端 seq；同用户任务的日志恢复由平台负责。游标过期返回 CURSOR_EXPIRED 和需要重新同步的指示，客户端重新读取授权快照再建立新游标。逐 token 输出不属于持久恢复契约。

## 5. 结果、工件与受控写入

AgentResult 是领域执行结果，不等同根验收。验证顺序：schema/version → principal/任务链/技能绑定 → 资源归属与版本/hash → 业务事实/证据 → 必要的模型语义验证。Verifier 失败只重试验证，不自动重放业务操作。已经接受的 result_id 对应不可变内容；修订分配新 result_id，保留来源关系。

工件使用 service/resource_type/resource_id 与版本/hash，由归属服务授权读取；不携带任意下载 URL、本机文件路径或长期凭据。引用必须至少提供 revision 或 content_hash；两者都有时都校验。M1 可以只使用现有简历内容 hash，无需假设数据库已有 revision。内容读取、生成预览、保存版本分别对应 resume_text、resume_preview、resume_version；result 的 skill_id 与原委派必须相等，ready 必须有工件和来源。

首期 skill allowlist：`job.resume.read.v1` 读取明确选中的本人简历，`job.resume.tailor.v1` 生成预览并在获得精确授权后保存新版本。只读禁止写业务资源，允许任务/审计/结果元数据。技能专属正文和 A2A capability descriptor 在 J04/J07/B05 定义，不能把 envelope 通过校验视为任意技能获准；M1 部署默认只启用 read。任意远程地址、额外技能和旧写工具都不因契约存在而开放。

ActionProposal 必须由实际执行方生成并持久化，绑定 operation、用户、任务链、executor/audience、资源版本、精确参数 hash、不可变预览工件、摘要和 expires_at。展示预览不能替代验证其实际内容 hash。ApprovalDecision 从认证用户产生，绑定 proposal_id/proposal_revision/operation/hash；浏览器自填 principal 不能决定审批者。

执行前检查用户状态、授权仍有效、资源版本、当前参数 hash、提案有效期以及完整绑定。批准不是直接执行命令；短期 grant 通过受控服务通道交付，不放进模型上下文或事件。grant 的签名/交换封装依赖 B03，不在本刀猜定格式；其业务约束必须覆盖这些提案字段。在执行端同一事务中完成 grant/operation 消费、业务写入、回执与 outbox。重复已完成 operation 返回原回执；任何内容/版本变化须重新提案和授权。撤销不能回滚已提交事务。

## 6. 预算与错误

父 reservation 向子 reservation 分配确定上限，按 principal/platform_task 绑定。子 deadline 不晚于父 deadline，累计预留和已结算消耗不超过父上限。参考预算样例的 already_reserved **包括已消耗和其他尚未释放的预留**，每一维均检查。实际预留须使用数据库原子更新，不能依赖先读后写。恢复/重试不重置额度；远端 usage unknown 保留预留，人工/自动核对后再结算。reported/estimated/unknown 必须向调用者说明，不能把估算当账单。

错误 code 是稳定机器字段，message 是裁剪后用户说明。retryable 只说明安全候选，实际重试还受 deadline、预算、幂等与副作用核对约束。EFFECT_UNKNOWN 必须 retryable=false、effect_status=unknown；只能查询核对，不能盲目再执行。HTTP/A2A 状态码映射随 endpoint/B05 冻结，不能在 B02 把暂未实现的 API 宣称已可调用。

## 7. 版本、解析与实现边界

Draft 2020-12；顶层封套 schema_version 固定 1.0，identity 是可嵌入上下文，版本由其 v1 schema/外层封套决定。additionalProperties=false，未知字段和版本拒绝。即使新增可选字段也会被旧读者拒绝，因此必须协商新版本、同时发布 schema/样例/消费者支持清单，不能原地扩大已冻结 v1。部署端先发布可读新版本的消费者，再切换发送方；不做隐式降级。仓库文件加 tag/commit SHA 后供两个仓库消费，禁止复制后自行修改。保留旧版本直到全部活跃/可恢复任务清空。

只接受 UTF-8 JSON，拒绝重复键、NaN、Infinity。整数限制在 ±2^53 可安全表示范围内（本契约计数均非负）；时间为 UTC `YYYY-MM-DDTHH:mm:ss[.ffffff]Z`，可选小数最多 6 位，不支持闰秒。format 必须开启断言，并使用真实日期检查；只匹配正则不够。schema 本地自包含，不允许网络 `$ref`；`$id` 使用不可访问的 .invalid 命名空间。

本刀提供结构验证以及 6 类语义决策的独立 Python/Java 参考实现。语义样本假定已完成结构验证且输入事实可信；没有实现令牌验签、数据库隔离/事务、原子去重、JCS、租约 fencing、A2A、SSE 服务或业务运行时。后续任务必须用实际服务/数据库的失败注入测试验证这些性质，不能仅复用此处纯函数得出生产安全结论。

参考：[JSON Schema 2020-12](https://json-schema.org/draft/2020-12)、[RFC 8785 JCS](https://www.rfc-editor.org/rfc/rfc8785)、[networknt 1.5.6](https://github.com/networknt/json-schema-validator/tree/1.5.6)。

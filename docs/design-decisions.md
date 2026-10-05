# Design Decisions

本文记录 TikiAgent v1.0 的关键设计不变量。它们不是开发时间线，而是正式系统在扩展 Specialist、Tool 或持久化后端时应继续保持的边界。

## Result 与 Verification 身份链

Verification 不能只记录 `passed=True`。每份报告必须绑定：

```text
result_id
handoff_id
subject_agent
```

正式 Supervisor FINISH 只认可每个 Todo 的最新 Result、身份匹配的审核报告，以及绑定这条身份链的 Supervisor 接受决定。Verifier 的 PASS/FAIL 是符合性意见，不直接完成或失败 Todo；Supervisor 可以明确带限制接受质量不足的有用交付，但不能覆盖身份、证据真实性、权限与实际文件边界。限制保存在 History/TaskBoard，最终回答必须披露。旧接受决定不能用于新 Result，Verifier 也不能绕过 Supervisor 直接结束 Workflow。结构化基线继续使用原 PASS 门控。

## 两层 Agent Context

正式 Agent 调用包含两层上下文：

```text
ContextBuilder 生成的 Base Context
        +
单次 Agent.run() 内部的 Local ReAct Memory
```

Base Context 来自 State、Task Board、Retriever、History 和 Notepad。Local Memory 只服务当前 Specialist 的本次执行，保存 Local Summary 与最近完整 ReAct interactions。

切换 Agent、Scope 或重新委派时重新构建 Base Context，不继承上一位 Specialist 的内部 messages。这样 ResearchAgent 的搜索思考过程不会污染 CodeAgent；CodeAgent 只获得结构化 ResearchResult、Supervisor instruction、验收条件和显式失败证据。

## ToolCall 与 ToolResult 原子性

Local Memory 的最小单位不是任意一条 message，而是完整 interaction：

```text
assistant ToolCall
        +
全部具有匹配 tool_call_id 的 ToolResult
```

裁剪 recent window 或执行 Local Compression 时不能拆开这组关系。ASK 阶段只有待批准 ToolCall，因此它属于 Pending Execution/Approval State；只有执行完成并产生 ToolResult 后，才能写入 Local Memory。

## Context Compression

Base Context 和 Local Memory 解决不同增长来源，因此使用独立 Compressor：

```text
Base Compressor  → 压缩旧 History 与编排上下文
Local Compressor → 压缩当前 Agent.run() 产生的 ReAct interactions
```

正确语义是 Summary 替换被压缩原文，而不是 `raw history + summary`。Task、验收条件、当前 Todo、最新失败和身份引用属于受保护事实。

Notepad 不是 Summary。Summary 为当前窗口节省 Token；Notepad 保存经过确认、以后不能忘记的长期事实，并作为独立 Context Source 参与检索。

## Tool Selection、Exposure 与 Permission

三者职责不同：

```text
Tool Selection
= 当前 Agent/Phase 向模型展示哪些 Tool Schema

Tool Exposure Guard
= 执行侧拒绝调用未展示的工具

Permission
= 已展示且参数合法的调用是否 ALLOW / ASK / DENY
```

Dynamic Tool Selection 可以降低 Tool Schema Token，但不能替代 Permission。Workspace Boundary、Timeout、Environment 和 Output Limit 仍在 Runtime 中机械执行。

## Capability Isolation 与 Runtime Isolation

Capability Isolation 限制不同 Agent 拥有哪些工具。例如 ResearchAgent 没有文件写入能力，Verifier Registry 没有 `write_file` 或 `edit_file`。

Runtime Isolation 限制已允许工具的实际执行边界，例如 Workspace、cwd、argv、env、timeout 和 output limit。v1.0 提供 Workspace/Runtime Enforcement，但不是操作系统级 Sandbox。

## Approval 与执行事实分离

Approval 表示某个绑定 Scope 和参数 fingerprint 的 ToolCall 已获批准；它不等于工具已经开始或完成执行。

持久化顺序固定为：

```text
ASK
→ save Checkpoint(awaiting_approval)
→ receive Approval Decision
→ validate scope + fingerprint
→ save Checkpoint(executing)
→ handler()
→ save Checkpoint(completed)
```

必须先持久化 `executing` 再启动可能产生副作用的 handler。这样崩溃恢复时不会把已经开始的调用误认为尚未执行。

## Recovery 不猜测副作用结果

Checkpoint 是 Resume source of truth，Trace 不能参与恢复决策。进程在 `executing` 阶段崩溃后进入 `recovery_required`，禁止自动重放。

人工恢复只有两类事实：

```text
confirmed_not_executed → 允许创建新的 execution attempt
confirmed_executed     → 进入 awaiting_reconcile
```

`confirmed_executed` 不能伪造成功 ToolResult。操作者必须提交与 execution identity 绑定的真实 ReconcileResult 和 evidence，Workflow 才能继续。

## Session、Turn、Task 与 Run

```text
Session = 多轮对话边界
Turn    = 一次用户输入和系统响应
Task    = 一次需要完成的业务目标
Run     = Workflow 的一次实际执行尝试
```

Session 只保存身份、Turn 计数和 active checkpoint reference，不内嵌完整 History。第二轮可以通过同一 Session 的显式 Result reference 使用第一轮结果，但不能继承上一轮 Specialist 的内部 messages。

## History、Checkpoint 与 Trace

```text
History    = 未来 Agent 可能再次检索的 Handoff、Result、Verification 和重要证据
Checkpoint = 崩溃恢复所需的权威 Workflow/Agent 执行快照
Trace      = Harness 实际执行生命周期的审计记录
```

完整长 stdout、网页正文等原始结果应进入 Artifact 或 Trace；History 只保留可复用结构化结果与关键证据。恢复只读取 Checkpoint 和它引用的持久化 History，不扫描 Trace 猜测状态。

## Event Stream 与显示投影

Workflow 和 Harness 分别发布自己真实产生的事件，再由统一 EventBus 生成 sequence、脱敏、截断并分发。ApplicationController 不能根据最终结果反推 Tool、Approval 或 Verification 事件。

TUI 使用：

```text
Application Event
→ TuiEventAdapter
→ TuiViewState
→ Widget
```

`TuiViewState` 是可丢弃显示投影，不是 Workflow State 或恢复事实。Worker 线程通过 Textual Message 回到主线程更新 Widget，不直接操作界面。

## Finalization

Finalization 只清理 ephemeral runtime memory，例如 local summary、recent interactions 和 candidate context cache；它保留任务状态、最终 Result、History、Artifact references、关键 Notepad 和 Task Board 最终状态。

最终用户回答由最新且验证通过的结构化 Specialist Result 确定性生成，不暴露内部身份 ID。回答使用独立长度预算；完整网页 snippet 继续保存在 ResearchResult 证据中，避免超过 Final History 字段限制而阻塞 Workflow 收尾。

## 调查与交付分别验收

正式工具型 Supervisor 的 Todo 使用 `artifact`、`inspection`、`environment` 模式，随 Handoff 固定，Result 不能自行降级验证要求。只读 Profile 展示读取、列表、搜索与环境元数据查询。VerifierAgent 通过独立 Harness 读取证据、按需检查环境与测试，逐项提交报告；Gate 不再根据扩展名强制套用 Artifact 检查。旧 SupervisorPlan 的 `code_task_mode` 和规则验证器保留用于基线兼容。

当前语义验收由模型完成，不保证结论绝对正确；程序保证必需标准完整覆盖、引用实际读取且可用的证据、结果身份一致。仅凭摘要或失败工具结果不能通过。宿主运行位置通过本地 `/paths` 展示。导入和测试入口可能执行代码，不等价于 OS 沙箱。边界见 [验证契约](verification-agent.md)。

## 有限执行与恢复预算

正式 Supervisor 使用编排工具循环，历史基线仍保留结构化决策方式。状态中新增 Supervisor 私有消息、待完成委派和按结果身份索引；原始验收要求与真实 Todo 状态不由模型任意覆盖。History/Local 压缩使用有界 LLM 摘要，失败保留原输入，不把历史摘要放入 system。详细取舍和恢复边界见 [Supervisor 与上下文](supervisor-context.md)。

正式 ReAct Runtime 分别限制模型步骤和实际工具次数。`loop_guard` 保存在 ReActRunSnapshot，记录次数与参数指纹对应的失败计数；拒绝、无效参数、命令非零退出和超时均占用预算。相同参数的成功结果会清除该参数的失败计数。每次执行默认最多 24 个工具结果，任务累计最多 60 个；Supervisor 负责结束判断，Graph 对继续委派另做强制预算检查。

ASK 在收到真实 ToolResult 前不计完成调用；Resume 从同一 Checkpoint 恢复已完成消耗，审批执行完成后计入一次，不重复执行。旧 ReAct Snapshot 缺少 `loop_guard` 时，至少从已保存 ToolResult 恢复调用数。旧 State 补入任务计数 0、上限 60；历史快照中未记录的早期委派消耗不靠 Trace 推断。旧计划和 Handoff 默认 `artifact`，不擅自改变冻结的任务要求；需要新只读验收时重新提交任务。

超预算停止保留真实 ToolResult，未完成的一轮不会写入 LocalMemory 或继续发给模型。模型 HTTP 请求另设有限 timeout/retry；命令工具的 Python 别名绑定启动 TikiAgent 的解释器，并返回真实执行 argv，避免 Windows 的 `python3` 缺失或系统 Python 与 uv 环境混用。未知命令仍按 Permission 拒绝。

# Supervisor 与上下文

正式应用使用 `PlanningSupervisorAgent`。它通过 Function Calling 使用编排工具，LangGraph 承担 Specialist 执行、验证与 Resume Entry。旧 `SupervisorAgent` 保留给结构化基线示例，不是正式 CLI/TUI 的入口。

## 计划与执行

Supervisor 可以创建多个 Todo，同一个 Specialist 可以负责多项。每项包括 `todo_id`、目标、owner、交付模式与依赖。计划更新不能删除已有项、重写已执行项、改变既定验收模式或原始验收标准；循环依赖和不存在的依赖会被拒绝。

| 工具 | 执行边界 |
|---|---|
| `update_plan` | 更新尚未执行的步骤或添加工作项，状态由程序保留 |
| `read_history` | 分页读取当前任务、或 Application 显式授权的同 Session 历史 |
| `delegate_task` | 校验 Todo、依赖、引用和预算后把控制权交回 Graph |
| `finish_task` | 检查每个 Todo 的最新 Result/Handoff/Verification 身份链 |
| `stop_task` | 记录阻塞原因，停止继续委派 |

这些工具经过 Exposure、参数验证和独立允许列表的 ExecutionHarness，不向 Supervisor 暴露文件修改或命令执行工具。委派和结束操作必须独立调用；包含这些操作的多调用批次会整体拒绝，避免执行部分动作后丢失其余调用。

模型生成的 `delegate_task` 先保存在 `supervisor_runtime.pending`，尚不形成完整 LocalMemory 交互。Graph 创建并绑定 Handoff，运行 Specialist，再经过 Verifier。结果回来后，程序将 Result 摘要、准确身份及 Verification 作为对应 ToolResult，补成完整交互。子 Agent 的内部消息不传给 Supervisor。

`results_by_id`、`verifications_by_id` 保存身份索引；按 Agent 的 `specialist_results` 继续作为最新结果视图。完成检查和多 Todo 最终回答均逐项查验，不能用另一个 Todo 的 PASS 代替。

Supervisor 默认最多 32 次模型步骤、64 个编排调用；相同工具和错误类别连续失败三次停止，成功调用清除连续失败计数。另有 Workflow 委派预算和 CodeAgent 工具预算。Verifier 提供失败类别和可重试提示；模型可以据此调整或停止，硬预算不能被重新规划绕过。

## 输入排列与隔离

每次模型调用按以下顺序重建输入：

1. 系统规则、角色、阶段规则、结构化输出要求。
2. 当前任务、委派指令和验收标准。
3. Notepad、History 摘要、相关历史。
4. 准确的当前 TaskBoard。
5. Local 摘要，以普通数据消息提供。
6. 最近完整的 assistant/tool 交互。

最新结果和验证保留在相关 History 与近期委派 Observation 中。系统规则不交给摘要器改写；TaskBoard 不用自然语言摘要替代。长 ResearchResult 的 summary/snippet 可以使用有标记的显示摘录，身份和 URL 保留，原文仍在 History。其他硬性记录过大时会明确报超限，不静默删除约束。

Tool Schema 通过 API `tools` 参数发送，仍计入上下文预算。消息列表和工具列表顺序由本地确定，供应商如何把工具定义编码进模型输入不由参数书写顺序决定。

作用域切换时重新构建 Base Context。Supervisor 的 LocalMemory 属于当前 Workflow；Specialist 的 LocalMemory 属于当前一次委派。

## 两层摘要

History 压缩器处理较早的非保护记录，默认保留最近一条软性记录以及全部受保护引用。Local 压缩器处理旧完整交互，优先保留最近四组，并按近期 token 预算缩小组数，至少留下最近一组。调用和对应全部返回值不能拆开。

两类 LLM 摘要都结合当前任务，输出已完成事项、关键决定、未解决问题、失败原因、下一步建议和来源引用。程序验证来源、长度及估算缩减效果，并独立保留 Local 工具结果中的错误码、退出码、超时、路径等事实。摘要不能修改审批、TaskBoard、Result 或 Verification。

旧摘要与待压缩原文共同参与下一次总结，新摘要替换旧输入。摘要来源和覆盖范围保存在 Context/LocalMemory；原始 History 不被改写。相同作用域、任务、来源和内容的摘要可复用，缓存有数量上限。

摘要请求每次最多四批，每个 Runtime 默认最多八次摘要方法调用；底层结构化解析仍可能做一次格式修复，HTTP 重试由模型配置约束。批次输入也检查预算。超长输入、无效引用、摘要未缩短或 API 失败时保留原上下文；相同失败输入不反复请求，总预算仍超限则停止。这里不保证 LLM 摘要完全无损。

默认总窗口 32,000 token，输出预留 2,000；Base/Local 软预算分别为 12,000/10,000，近期交互目标 6,000，总输入达到可用窗口 85% 时提前尝试压缩。统计包含实际 messages、Tool Schema 和结构化 Schema；当前 estimator 为保守字符近似，不是后端精确 tokenizer。供应商最终请求及结构化修复重试还会检查输入预算。

## 暂停与恢复

CodeAgent 审批暂停时，现有 Checkpoint 的 WorkflowResumeSnapshot 同时保存 TaskBoard、按身份索引的结果，以及 `supervisor_runtime`：私有消息、待完成委派、计划版本、预算消耗、摘要缓存和摘要调用次数。ReActRunSnapshot 保存子 Agent 的 Base Context、LocalMemory 和待执行工具。

新进程读取 Checkpoint 和 History 引用，从 Graph Resume Entry 恢复 CodeAgent；验证完成后补齐原 Supervisor 委派的 ToolResult，再继续规划，不能重新派发已暂停的同一任务。旧快照的缺失字段有兼容默认值，已有最新结果按原身份补建索引。

这仍是现有工具执行检查点的恢复语义。未实现每个 Workflow 节点自动落盘，也不保证 ResearchAgent 在任意进程崩溃点恢复。Trace 只用于审计，不参与恢复决策。

成功 Finalization 后清空 Supervisor 的临时 LocalMemory 与摘要缓存，保留 TaskBoard、结果、验证、History 和预算统计。

## 观察与验证

编排调用与上下文准备事件经统一 EventBus 脱敏分发，并进入 Session Trace。`context_prepared` 包含分项预算、压缩前后用量和摘要结果；TUI 只在发生压缩或摘要问题时展示 Context 卡片。Controller 不反推这些内部事件。

离线回归：

```powershell
uv run --locked python -m pytest
```

小规模真实验证会产生模型 API 费用；只使用临时 Workspace 和人工构造历史，不读取已有 Session：

```powershell
uv run --locked python examples/validate_supervisor_context.py --env-file .env --live
```

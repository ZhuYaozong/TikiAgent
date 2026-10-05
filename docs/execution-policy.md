# 执行预算与进展策略

预算控制分为模型输入/输出、单 Agent 工作次数、任务累计资源三个层次。达到工作上限时进入一次性收尾，而不是再开放工作工具。

## 配置与责任

- `runtime/policy.py`：应用默认值与环境覆盖。Chat 输出 8,192、Router 输出 2,048、摘要输出 8,192 token。
- `providers/llm/staged.py`：按阶段创建配置视图，明确关闭 SDK 隐式重试，实际重试逐次计数；收尾不重试。
- `context/preparation.py`：按照当前阶段输出额度计算可用输入窗口；不可压缩的任务约束过大时停止。
- `harness/persistence/budget.py`：任务资源消费凭据，先持久化再请求；恢复不能增加原任务额度。
- `runtime/guard.py`：重复失败、重复读取、重复测试和拒绝记录；相同参数指纹忽略超时和输出长度等非实质变化。
- `verification/gate.py`：Result/Handoff 身份与验收门控。有证据的部分研究交付可以被审查，但不自动通过。

## 收尾与恢复

128 次任务模型额度中，普通请求最多消费 112 次，16 次保留给各 Agent 的收尾。输出截断、结构错误、供应商错误和工具预算耗尽是不同事实；Trace/Result 保存脱敏诊断，不保存异常响应正文。已有来源但总结失败时不允许重派搜索来刷新预算。

`budgets/<scope-hash>.json` 保存作用域、模型/工作/联网计数、冻结上限及联网参数指纹。它只记录资源消费，不恢复 Graph。原子替换配合排他锁避免同时扣款；锁遗留时保守停止，不猜测请求是否发送。网络请求是否真正计费以供应商为准，这里统计的是尝试次数。

`checkpoints/finalizations/` 保存一次性收尾凭据。审批暂停后的 Workflow/Agent 状态继续由已有权威 Checkpoint 恢复。没有新增任意节点崩溃自动恢复，也不能从 Trace 拼接出恢复状态。

## 去重边界

- Research 拒绝同任务相同联网参数；连续搜索没有新增来源时提前收尾。不同措辞但语义相同的查询尚不做可靠识别。
- Code 已通过的同参数测试在 Workspace 版本不变时拒绝重复执行。文件变化后可重新测试。
- Verifier 可以复用自己读取的同版本证据，不能复用 CodeAgent 的结论来替代独立检查。
- 权限拒绝不因其他只读动作而消失；Task 预算和单次执行防重共同约束委派与恢复。

Workspace 版本使用文件相对路径、大小和修改时间，不是内容哈希；保留相同大小和时间的外部改动可能无法识别。超过遍历上限时禁用复用，优先重新验证。当前仍为单机文件实现，不承诺分布式 exactly-once。

## 验证

默认测试完全离线，覆盖阶段输出、收尾保留额度、跨实例消费恢复、锁冲突、来源身份、同版本取证复用、改动后重测、拒绝持久性以及部分成果进入真实验收。真实 API 回归使用隔离 Session/Workspace 和全局请求上限，不能用单次成功代表稳定成功率。

## DeepSeek 与可配置上下文

| 阶段 | 最大输出 token | DeepSeek 思考强度 |
|---|---:|---|
| router | 2,048 | none（关闭） |
| chat | 8,192 | low |
| supervisor | 16,384 | high |
| code_agent | 32,768 | high |
| code_final | 16,384 | low |
| research_agent / research_final | 16,384 | low |
| verifier | 16,384 | high |
| verifier_final | 16,384 | low |
| summary | 8,192 | low |

通过 `TIKI_OUTPUT_<STAGE>`、`TIKI_THINKING_<STAGE>` 覆盖；输出额度包括供应商计入的推理与可见输出，不保证所有额度都用于可见文本。`TIKI_LLM_API_STYLE=auto` 仅自动识别 DeepSeek 官方域名；其他兼容后端默认不传专用参数，显式 `deepseek` 才启用。模型名称仍来自 `.env`，不自动换模型。

接口模型 ID 与控制台显示名称不一定相同。示例使用当前官方 Flash 接口标识 `deepseek-flash`，不要把 `DeepSeek-V4.1-Flash` 这样的显示名直接放进请求；服务升级后应核对服务支持列表。[官方思考模式与工具调用协议](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode/)。

应用主动使用 131,072 总窗口，不追求填满模型最大窗口。Code 可用输入约 96,304，Supervisor/Research/Verifier 约 112,688；计算式为总窗口减输出预留减 2,000 安全余量。阈值是工程起点，不是所有 Agent 的行业标准。

`TIKI_CONTEXT_BASE_BUDGET=32000`、`TIKI_CONTEXT_LOCAL_BUDGET=48000` 是软压缩阈值；`TIKI_CONTEXT_RECENT_INTERACTIONS=8`、`TIKI_CONTEXT_RECENT_TOKENS=16000` 控制近期完整交互；`TIKI_CONTEXT_COMPRESSION_RATIO=0.85` 控制总输入的提前压缩。配置会在启动校验；硬约束无法放入预算时拒绝发送，摘要失败不循环总结。

新任务使用新默认值。已有任务账本上限取旧值与新值的较小者；审批快照中的 Code 工作轮数也继续遵循旧值，不能通过重启扩大执行额度。调整全局配置不会自动恢复已经失败的任务。

## 有界真实冒烟

```powershell
# 默认只说明配置，不调用 API
uv run --locked python examples/live_budget_smoke.py
# 显式授权真实 API；新建独立测试目录，不读取既有会话
uv run --locked python examples/live_budget_smoke.py --run-live --env-file .env
```

Research、标准库 unittest Coding、Research→HTML Hybrid 各执行一次；整批最多 128 次模型请求（保留16次收尾）与20次联网，不自动重跑失败任务。每场景480秒后在请求入口阻止新的工作请求；这是软截止，不强杀正在进行的 API 或工具，已发请求仍受自身超时控制。遇到 ASK 只记录暂停，不自动批准。独立验收检查来源来自实际 Observation、文件存在、标准库测试非空并通过，以及 HTML 引用来源；新闻事实仍需人工核对。

每次测试在 `.tiki-demo/budget-smoke-<ID>/` 保存隔离 History/Trace、最终可见答案和 `report.json`。报告只包含安全指标和公共来源，不保存原始思考内容。阶段诊断保留输出上限、finish_reason、usage；SDK 返回的 reasoning_tokens 等计量字段可能不可用，不把缺失值当零。

一次已完成的实测、独立检查及限制见 [预算配置验证](validation-budget-smoke.md)。

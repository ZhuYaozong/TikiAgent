# Conversation-first TUI

TikiAgent TUI 以用户问题和最终回答为主视图。它借鉴 Claude Code 与 MokioAgent 的终端信息层级，但继续使用 TikiAgent 自己的 Application Event、Session、Checkpoint、Approval 和 Workspace 边界。

## 启动

```powershell
uv run --locked tikiagent-tui --data-dir .tiki --env-file .env
```

连接已有 Session：

```powershell
uv run --locked tikiagent-tui `
  --data-dir .tiki `
  --env-file .env `
  --session-id <SESSION_ID>
```

## 信息层级

```text
Application Event
        ↓
TuiEventAdapter
        ↓
TuiEventPresenter
        ↓
TuiViewState.feed
        ↓
Textual Feed Card
```

`TuiEventPresenter` 只选择用户需要的脱敏、有界字段。Widget 不解析 Workflow State，也不直接读取原始 ToolResult；命令输出和文件内容只通过显示投影提供有限预览。Session、Workflow start/completed 等低价值事件只更新顶部与侧栏，不重复污染主对话。

主 Feed 包含：

- 用户问题卡片；
- Supervisor / Agent / Handoff 的紧凑进度；
- 同一 ToolCall 原地更新的工具卡片，折叠标题也显示目标与主要结果；
- 只读 TaskBoard 进度，以及关联最新 Result 的验收状态；
- Verifier 的逐项审核意见、证据及缺口，和 Supervisor 的独立验收决定；
- 完整 Markdown 最终回答与来源链接。

### 工具与任务进度

`requested → running → completed` 更新同一张卡片，不追加三张日志。调用按 Session/Task/Agent/Run/ToolCall 隔离；新的 execution identity 区分实际执行尝试，审批恢复继续更新对应卡片。完整事件顺序仍用于观察与审计，显示合并不参与 Checkpoint 恢复。

文件工具展示路径、写入/替换量、目录或匹配数量；联网工具展示查询、来源标题和 URL；命令展示 argv、cwd、退出码、耗时及有限 stdout/stderr。工具返回不等于业务成功：权限拒绝、预算拦截、非零退出和超时明确显示并默认展开。ASK 只显示“待审批”，不伪造 ToolResult，也不提前显示“执行中”。

TaskBoard 是现有 Workflow 事实的只读显示投影，没有第二套 Todo 存储。展开任务清单可查看 owner、状态、尝试次数、最新 Result/Verification 引用和验收限制。过期审核标记为历史信息，不代表当前交付已验收。侧栏“请求数”统计当前 Turn 中观测到的唯一调用请求，覆盖 Supervisor、Research、Code 与 Verifier；不是实际执行次数、账单计量或跨进程累计次数。

Agent 开始事件在真实 Graph 节点入口产生，不根据 `current_agent` 的下一站推断。Resume 同样逐节点发布事件。任一 Todo 带限制接受，后续 Todo 的正常接受不能抹掉整体“含限制”。

位于底部时 Feed 自动跟随；向上阅读时不强制滚到底部。已有卡片更新保留展开选择；新出现的错误或审批会展开一次。Worker 仍只发送 Textual Message，所有 Widget 修改在主线程执行。

显示文本与列表有限长，并遮蔽已识别的凭据和终端控制字符，不展示 `reasoning_content`。文件长度和匹配数量在事件截断前计算；预览不等于完整原始内容，任意内容中的秘密识别仍不能保证。

### 验证显示链路

离线事件投影、真实 Checkpoint 审批恢复和 Textual 主线程/滚动回归覆盖在 `tests/test_execution_visibility.py`。真实 API 验证是显式启用的测试，读取本地 `.env`，创建隔离的临时 Session，不修改现有 `.tiki`：

```powershell
$env:TIKI_RUN_VISIBILITY_LIVE = '1'
uv run --locked python -m pytest tests/test_execution_visibility_live.py -s
Remove-Item Env:TIKI_RUN_VISIBILITY_LIVE
```

该测试包含 Research、Coding、Hybrid 三个小任务，会产生模型和 Tavily 调用费用；默认测试运行不调用 API。

2026-10-05 使用本地配置完成真实验证：Research、Coding、Hybrid 均得到 `workflow_completed`；对应唯一工具请求/工具卡片数量为 13/13、10/10、21/21，Todo 数量为 1、1、2。该记录验证显示链路，不是任务成功率 Evaluation，也不保证其他运行产生相同调用数量。

## 最终回答

`FinalAnswerComposer` 在 FINISH Guard 通过后确定性组合结构化 Result：

```text
ResearchResult
├── summary
├── findings
├── sources
└── unresolved_questions

CodeResult
├── summary
├── changed_files
└── tests_run
```

Composer 会再次检查 Result 与 Verification 的 `result_id/handoff_id/subject_agent` 关联，但不会把这些内部 ID 输出给用户。第一版不额外调用 LLM 润色，避免额外成本和来源幻觉。

最终回答受独立长度预算约束，保证能够安全写入 Final History。ResearchAgent 抓取的完整网页摘录继续作为结构化 Result 证据保存在任务 History 中；用户回答只展示经过整理的总结、关键发现、来源标题和 URL，避免长网页正文阻塞 Workflow 收尾。

如果 Worker 仍遇到未处理异常，TUI 会把错误作为 `Operation Failed` 卡片显示在主 Feed，而不是只在顶部短暂显示或静默停止。

## 快捷键与命令

```text
Ctrl+B           显示或隐藏侧栏
Ctrl+N           新建 Session
Ctrl+Q           退出；不会取消 Workflow
F5               刷新只读 Workspace

/new [workspace] 新建 Session
/session <id>    连接已有 Session
/status          读取权威 Checkpoint 状态
/approval        处理 Approval
/recovery        处理 Recovery
/workspace       刷新 Workspace
/paths           本地显示 Workspace、History/Handoff、Trace 与 Checkpoint 位置
/help            显示帮助
/quit            退出 TUI
```

Approval、Recovery 和 Reconcile 仍通过 Controller 进入权威 Harness/Checkpoint 流程。Feed、顶部状态和侧栏都只是可丢弃的显示投影，不能参与恢复决策。

## 人工审批

审批弹窗优先展示需要做出的实际决定：工具名、PermissionPolicy 的审批原因、Workspace 与完整规范化参数。`run_command` 额外展示结构化 argv、Workspace 内的相对工作目录和超时。参数按原顺序逐项传给程序，不拼接成 Shell 字符串；`python` 别名在实际运行时仍使用启动 TikiAgent 的解释器。

长内容可以滚动，稍后、拒绝和批准按钮固定在底部。ToolCall、Checkpoint、Revision、Session 和规则编号收进“技术详情”。API key、密码、认证头和已识别的敏感命令参数会在显示投影中遮蔽，原执行参数不变；这不是任意内容的秘密识别器。

详情经 `Checkpoint → WorkflowOutcome → ApplicationOutcome → TuiViewState → ApprovalPrompt` 传递，不通过 Trace 推断，也不经 EventBus 文本截断。连接已有 Session 或执行 `/status` 会重新读取当前 Checkpoint。详情缺失或请求身份不匹配时禁止批准，可选择稍后并刷新状态；拒绝仍通过同一 Controller 处理。

点击“批准本次”只提交当前 request_id 和 expected_revision，Checkpoint 的 scope/fingerprint 与一次性消费规则继续负责实际授权。弹窗不持有第二套审批状态。

`/paths` 根据当前启动的 `--data-dir` 和 Session 生成位置展示，不调用 Controller、EventBus 或模型，路径不进入 Transcript/History。它说明配置的存储位置，不替代 Checkpoint 状态判断。交付的网页位于 `workspaces/<session_id>/`；handoff 是 `histories/<session_id>.jsonl` 中的 `record_type="handoff"` 记录，并不是 Workspace 内的文件。

只读调查不运行测试，也不为了通过验证而创建报告。工具预算耗尽或同一参数重复失败时，执行器返回带证据的未完成结果，Supervisor 停止委派；主 Feed 显示最终失败原因，用户可调整任务后重新提交。`Ctrl+Q` 仍只关闭界面，不能保证后台命令已经取消。

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

`TuiEventPresenter` 只选择用户需要的安全字段。Widget 不解析 Workflow State，也不会读取原始 ToolResult stdout。Session、Workflow start/completed 等低价值事件只更新顶部与侧栏，不重复污染主对话。

主 Feed 包含：

- 用户问题卡片；
- Supervisor / Agent / Handoff 的紧凑进度；
- 默认折叠的 Tool 执行摘要；
- Verification PASS/FAIL；
- 完整 Markdown 最终回答与来源链接。

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

`/paths` 根据当前启动的 `--data-dir` 和 Session 生成位置展示，不调用 Controller、EventBus 或模型，路径不进入 Transcript/History。它说明配置的存储位置，不替代 Checkpoint 状态判断。交付的网页位于 `workspaces/<session_id>/`；handoff 是 `histories/<session_id>.jsonl` 中的 `record_type="handoff"` 记录，并不是 Workspace 内的文件。

只读调查不运行测试，也不为了通过验证而创建报告。工具预算耗尽或同一参数重复失败时，执行器返回带证据的未完成结果，Supervisor 停止委派；主 Feed 显示最终失败原因，用户可调整任务后重新提交。`Ctrl+Q` 仍只关闭界面，不能保证后台命令已经取消。

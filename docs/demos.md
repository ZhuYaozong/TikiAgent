# Demo Validation

`tikiagent-demo` 用三个冻结场景检查 TikiAgent 的主要产品路径。它执行真实 Application Controller，而不是另写一套 Demo Workflow。

## 场景

```powershell
uv run --locked tikiagent-demo research
uv run --locked tikiagent-demo coding
uv run --locked tikiagent-demo hybrid
```

- `research`：只调研近期 AI Agent 变化，要求带可追溯来源，不修改 Workspace；
- `coding`：创建标准库 `unittest` 可验证的计算器；
- `hybrid`：ResearchAgent 先交付结构化调研结果，CodeAgent 再生成带来源链接的 `comparison.html`。

先用 dry-run 检查冻结的任务、预期 Agent 和验收条件，不会初始化 Runtime，也不会调用模型或 Tavily：

```powershell
uv run --locked tikiagent-demo hybrid --dry-run --json
```

## 输出

默认数据目录为 `.tiki-demo`，每次运行写入独立目录：

```text
.tiki-demo/demo-runs/<DEMO_RUN_ID>/
├── application-summary.json
├── application-timeline.md
├── trace-summary.json
├── trace-timeline.md
└── demo-result.md
```

Application Event 与 Harness Trace 保持分离：

- Application 视图回答“任务在语义上发生了什么”；
- Trace 视图回答“Harness 的执行生命周期发生了什么”；
- Checkpoint 才回答“恢复时系统应从哪里继续”。

时间线不会复制 Application Event `data` 或 Trace `details`，避免把工具原始输出和敏感参数扩散到展示产物。Research-only 没有工具执行时，Trace 摘要可以合法地为 0。

Workspace 产物清单只记录 Session Workspace 内普通文件的相对路径，不读取内容、不跟随符号链接。

## 暂停与恢复

Demo Runner 不自动批准工具。如果结果为 `awaiting_approval`、`recovery_required` 或 `awaiting_reconcile`，进程以退出码 2 结束，并在 `demo-result.md` 与终端中给出下一条真实 `tikiagent` 命令。

恢复状态必须由 Session 引用的权威 Checkpoint 判断，不能从 Demo Summary 或 Trace 推断。恢复后的事件仍进入原 Application/Harness 存储；当前 v0.9b 不把恢复过程伪装成同一个同步 Demo 命令。

## 配置与成本

真实运行读取 `.env` 中的 OpenAI-compatible 模型配置；Research/Hybrid 还读取 Tavily 配置。可以通过参数覆盖路径：

```powershell
uv run --locked tikiagent-demo hybrid `
  --data-dir .tiki-demo `
  --env-file .env `
  --workspace-id demo-workspace
```

真实 API 会产生费用和外部请求。`.env`、`.tiki-demo/` 均被 Git 忽略。

## 这不是 Evaluation

Demo Validation 用来证明三条主路径可以运行并留下可审计结果，不提供任务集、重复采样、基线对比或统计结论。`elapsed_seconds` 只是该次运行的观测值，不能用于宣称成功率、Token 成本优势或 Multi-Agent 优于 Single-Agent。

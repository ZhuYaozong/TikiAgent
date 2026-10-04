# Changelog

本项目从 v0.1 开始保留可运行的架构演进基线。这里只记录面向使用者的发布变化。

## Unreleased

### Added

- 按 Agent/阶段配置输出额度，任务级模型与联网消费账本，收尾独立保留额度；详见执行预算文档。
- 正式应用接入 VerifierAgent：逐 Todo 验收、证据读取、受控验证工具与硬性 Verification Gate。
- Todo/Handoff 声明所需能力和冻结验收条件，新增 environment 模式和固定解释器的版本/导入查询。

### Fixed

- Research 收尾改用有界结论和真实来源 ID，区分总结失败与取证预算耗尽；有证据的部分交付不再跳过实际验收。
- 阻止未改变策略的重复委派、同版本重复测试与重复联网参数；Verifier 仅复用自身同版本证据。
- Result/Verification 采用有界模型视图，完整工具输出与验证证据不再重复注入 Supervisor；准确控制事实独立于可压缩历史保留。
- Workflow/CHAT 执行异常保存关联 Turn 的失败回复；会话上下文按最新回复成对组装，旧任务未记录结果时明确标为未知。
- 模型截断和无效工具参数最多重试一次，禁止执行不完整调用；递归文件工具逐项检查 Workspace 边界且不跟随链接目录。

- Verifier 取证预算耗尽后先保存完整交互，再进入一次仅提交报告的收尾；超预算批量调用返回明确拒绝结果，避免丢失证据或 ToolCall/ToolResult 配对。

- 空模型响应最多重试一次，并记录不含正文或隐藏推理的响应诊断；已知数值 token 统计不再作为凭据遮蔽。

- 为只读调查增加独立验收模式，避免因没有修改文件而反复创建报告；debugging 阶段支持新建缺失文件。
- 为正式 CodeAgent 增加实际工具调用预算和相同参数重复失败保护；暂停/恢复保留已消耗预算，停止时保留失败证据并返回最终原因。
- Python 命令别名绑定项目解释器；模型请求支持有限超时和重试配置。
- TUI 增加纯本地 `/paths`，展示 Workspace 与 History/Handoff 等存储位置。

### Changed

- 按职责分离 Agent 角色、单 Agent Runtime、多 Agent Orchestration、Verification、Tools、Providers 和 Interfaces；Context 与 Harness 按子域组织。
- 早期 ReAct Graph 和 Plan/Verify 实现归入 `baselines/`，正式应用启动不加载这些基线。
- 三个 CLI 命令名、配置方式和持久化 JSON 协议不变；直接导入旧 Python 子模块的调用方需要参考 [模块导航](docs/module-layout.md) 更新路径。保留的包级兼容导出采用延迟加载。

## 1.0.0 - 2026-08-25

### Added

- Supervisor、ResearchAgent、CodeAgent 与统一 Verification Gate；
- Context Engine：History、Retriever、Task Board、Context Profiles、Context Builder、Monitor、Compressor 与 Notepad；
- Execution Harness：Tool Exposure、Permission、Approval、Workspace/Runtime Enforcement、Checkpoint/Resume 与 Trace；
- Application Plane：Session、Turn、Intent Router、Event Stream、CLI 与人工恢复入口；
- Conversation-first Textual TUI，支持多轮 Session、Approval、Recovery、只读 Workspace 和 Markdown 最终回答；
- Research、Coding 与 Hybrid 三类可重复 Demo Validation；
- 产品型 README、项目 Logo、正式架构与 Design Decisions 文档；
- 使用 MIT License 发布。

### Safety

- Result 与 Verification 通过 `result_id/handoff_id/subject_agent` 绑定；
- ASK 在 Checkpoint 成功持久化后暂停，不生成伪造 ToolResult；
- `executing` 崩溃后禁止自动重放，必须人工 Recovery/Reconcile；
- 最终回答使用独立长度预算，完整网页摘录保留为 History 证据，避免长结果阻塞 Finalization；
- Event、Trace 与 TUI 投影执行脱敏和输出截断，不作为恢复事实。

### Known limits

- Workspace Boundary 不是操作系统级 Sandbox；
- JSONL 持久化面向单机 v1，不提供分布式 exactly-once；
- Token 预算使用字符近似值；
- Demo Validation 不是统计 Evaluation，不声明未经实验支持的量化收益。

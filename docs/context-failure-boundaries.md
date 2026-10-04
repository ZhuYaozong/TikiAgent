# 上下文与失败边界

## 模型视图不等于执行事实

History 保存原始 Result/Verification。ContextBuilder 为模型生成有界视图，不能把视图回写为原始报告：

- CodeResult 的长工具输出变为工具调用 ID、成功标志、错误码和证据索引；原文由 History 或 Verifier 的 `read_evidence` 读取。
- Verification 保留 Result/Handoff/Todo 关联、逐项验收状态、证据引用、失败分类和可重试标志；原因仅展示短摘录。
- `WorkingMemory.control_facts` 独立保留验证控制事实。压缩可以替换报告正文，不能覆盖这些事实。
- `delegate_task` 返回简短报告，避免将同一完整报告同时塞进 Base Context 和 Local Messages。

FINISH 仍检查真实 State 中的最新 Result、Verification 和验收覆盖，不根据摘要或视图自动通过。旧快照若没有独立控制事实，仍保留原有 `protected_refs` 保护行为。

系统规则、原始任务、当前指令、验收条件和 TaskBoard 不参加正文摘要。总输入仍包含 Prompt、Base、Local、工具 Schema；压缩后超预算则停止，不能靠丢弃硬条件伪装成功。

## 失败后的会话

已记录 Turn 的 CHAT/Workflow 调用异常，会产生失败 Response，保存受控 `error_category` / `error_stage`，不保存供应商异常正文。Controller 产生应用最终回复；Workflow Adapter 产生内部失败事件。

最近对话按 Turn 关联其最新 Response，审批后新回复替代旧的暂停显示。旧数据中没有 Response 的 Turn 标记为“状态未知”；这是输入视图，不会补造历史执行结果。当前用户消息优先，历史不是自动续跑指令。

Resume 遇到上下文预算或无效模型输出会保存失败回复并保留 Checkpoint 引用。scope/revision 校验失败仍作为操作错误返回，不伪装成已经运行的任务失败。Checkpoint 是恢复权威，Trace 仅用于审计。

这不是节点级 Checkpoint：进程硬退出或存储不可用时，不能保证写出失败 Response；也不保证保存所有节点内存状态。执行异常不代表文件或外部副作用已回滚，不自动重放工具。

## 模型和文件执行防线

`finish_reason=length`、工具参数不是完整 JSON 对象时，适配器合计最多重新生成一次；不向 Dispatcher 返回本批不完整调用。第二次仍失败则明确报错，不扩大模型预算。

递归 list/grep 对每个子项重新执行 Workspace 路径校验，不跟随链接目录。此机制不是 OS Sandbox，无法消除其他进程并发替换路径造成的竞态。

## 离线验证

`tests/test_workflow_context_boundaries.py` 覆盖长报告投影、控制事实保留、失败 Turn、最新 Response、截断/坏参数重试、Resume 保留引用、CLI 失败码和递归边界。全部使用假模型和临时目录，不调用真实服务、不重放用户任务。

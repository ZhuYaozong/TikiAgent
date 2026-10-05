# 研究委派与证据链

## 能力与委派范围

Supervisor 的能力说明由 `agents/capabilities.py` 注入，Research 的行为约束由 `context/profiles.py` 组装。工具表仍通过 Function Calling 发送，执行仍经过已有 Harness。`delegate_task.instruction` 是模型生成的自由指令，程序不使用自然语言关键词黑名单来猜测任务；结构化能力、Todo 身份、依赖和作用域由程序校验。

Research 具备 Tavily 搜索、正文提取及基于证据的总结/比较能力，没有浏览器、社交热度排名、本地命令或自行查询 History 存储的工具。Supervisor 不默认增加精确发布时间、全网最热门证明或自己猜测的答案。用户明确提出的数量、日期和排名要求仍须保留；信息缺失时报告限制，由 Supervisor 决定补做、带限制接受或停止。

`ContextRequest.current_todo_id` 在正式 Graph 入口绑定当前 Handoff。ContextBuilder 给 Specialist/Verifier 只注入这一个 Todo 及其验收条件，全局任务和标准另标为背景；Supervisor 保留完整 TaskBoard。无当前 ID 的旧教学入口仍保留 owner 视图，避免破坏示例兼容。作用域切换不继承另一个 Agent 的 LocalMemory。

新任务创建时保存带时区偏移的 `task_reference_time`，Supervisor、Research、Code、Verifier 均看见相同时间。现有 Workflow/ReAct Snapshot 保留该值；Resume 不刷新。旧快照缺少时间时恢复为未知，不根据恢复日推断旧任务的“今天”。此字段不是搜索日期过滤器，也不能证明文章日期。

## 证据来源

`agents/research_evidence.py` 在程序侧维护来源目录：

| 类型 | 接受条件 | 保留的身份 |
|---|---|---|
| search | 成功 web_search 返回的来源 | 实际 tool_call_id、query、URL |
| extract | 成功 web_extract 返回非空正文 | 实际 tool_call_id、URL、正文摘录 |
| history | 显式引用且作用域允许的原始 ResearchResult，身份和来源配对合法 | 原 History record_id、原 observation_id、URL |

指令中的 URL 本身不是证据。成功提取可以直接成为来源，不要求本轮再次 web_search。历史复用不依赖 Retriever 的软性 max_records 窗口：Graph 单独加载显式引用的原始 History，检查同 Session、当前 Task 或 Application 已授权跨 Task 引用；Research 再检查 Result ID、Handoff 引用与来源配对。不从 Trace、模型摘要或跨会话记录恢复来源。

Verification Gate 对 history 类型还检查原 Result 是否在本次 Handoff 引用中，以及原来源身份和 URL 配对是否一致。新来源 ID 必须由 URL 的稳定 SHA-256 前缀生成；逐条引用必须覆盖当前 findings 且指向存在的来源。旧 Result 没有引用映射时维持旧来源校验，不补造映射。

同 URL 的当前成功提取优先于搜索摘录或历史正文；失败提取不会成为成功 Observation，也不会抹除已有搜索证据。搜索成功仅代表发现来源，不代表读过全文或事实正确。

## 日期、摘录与总结

`published_date` 只透传供应商实际返回的字符串，无字段则为 null。正文中发现的日期保留为摘录，模型可结合明确的发布日期文字形成结论，但程序不将正文任意日期或 URL 中日期自动写为已确认发布日期。

目录最多 12 个来源，排序优先当前正文，其次显式历史，再按任务关键词匹配选择搜索项。每项最多 1400 字符原文片段：页首、日期字段附近、关键词附近和页尾；它是确定性摘录，不是 LLM 摘要，不保证相关性或完整性。工具已经截断的原文不能凭空恢复。中文关键词匹配和日期模式只是启发式，不能证明语义正确。

工作轮数和搜索/提取次数不增加。已有来源的新正文或实际日期元数据可以计为进展；连续两轮可识别的联网结果没有内容增量时提前总结。相同参数仍受原有去重保护。模型判断证据足够时可立即停止工作，不要求耗尽预算；程序不声称能确定性识别所有任务的“足够”。

收尾仍只有一次有界结构化总结：2000 字符总述、最多 12 条结论、每条 800 字符。模型只输出结论和 `source_ids`，程序回填来源，不接受虚构编号。结果保留 legacy `findings/sources`，新增 `finding_citations` 对应每条发现；Observation 增加 kind 和历史身份，来源增加 source_id、evidence_kind 和 nullable published_date。

正文提取失败作为缺口返回，已有证据仍可部分交付；截断显式披露。格式/总结失败保留已取得来源和诊断，不能通过再次搜索解决。缺少用户要求的日期、数量或排名等语义条件，由模型明确报告，Supervisor 作验收决定，不用规则伪造满足。

## 回传、恢复与展示

Supervisor 的 `delegate_task` 返回观察不只包含一句 summary：还包含有界 findings、逐条引用、来源元数据、delivery_status、stop_reason、finalization_status 和 unresolved_questions。完整证据在 History；不会回传 Specialist 内部 messages 或网页全文。最终回答用来源编号连接发现与 URL，并区分搜索摘录、已提取正文和历史来源。

上述 Result 写入原有 History，并进入已有 Workflow Snapshot；下游 Code 审批暂停后的新进程恢复保留 Research Result、引用、TaskBoard 和冻结时间。此次未增加 Research 任意节点崩溃恢复、Workflow 节点级 Checkpoint、新排名 API、浏览器或强沙箱。旧 JSON 可读，不补造旧数据缺失的日期、时间或引用。

离线回归包含直接提取、搜索日期缺失、正文晚出现日期、提取失败、授权历史复用、伪造/跨作用域引用、当前 Todo 隔离及真实 Research→Code 审批恢复：

```powershell
uv run --locked python -m pytest tests/test_research_context_evidence.py
```

这些测试验证程序契约和状态流，不代表新闻事实正确率或真实模型的稳定成功率。

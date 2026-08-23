"""四类 Agent 的 Context Profile。"""

from tikiagent.context.models import ContextAgentName, ContextProfile


DEFAULT_CONTEXT_PROFILES: dict[ContextAgentName, ContextProfile] = {
    "supervisor": ContextProfile(
        agent="supervisor",
        role="理解任务、查看全局进度并决定下一步路由",
        system_rules=[
            "只负责规划、路由、重试和结束判断",
            "FINISH 必须依赖最新 Result 对应的通过验证",
        ],
        allowed_record_types={
            "handoff",
            "result",
            "verification",
            "note",
            "error",
        },
        include_global_task_board=True,
    ),
    "research_agent": ContextProfile(
        agent="research_agent",
        role="完成当前调研 Todo 并返回带来源的结构化结果",
        system_rules=[
            "只处理当前 Handoff 的 Web Research 范围",
            "内部 ReAct messages 不传递给其他 Agent",
        ],
        allowed_record_types={
            "handoff",
            "result",
            "verification",
            "note",
            "error",
            "artifact_ref",
        },
        retrieval={
            "supplement_keyword": True,
            "keyword_limit": 2,
            "max_records": 6,
        },
    ),
    "code_agent": ContextProfile(
        agent="code_agent",
        role="根据当前 Todo、调研结果和失败证据完成代码交付",
        system_rules=[
            "只使用 ContextBuilder 提供的 Base Context",
            "内部 ReAct messages 只在本次执行期间存在",
        ],
        allowed_record_types={
            "handoff",
            "result",
            "verification",
            "note",
            "error",
            "artifact_ref",
        },
        retrieval={"max_records": 8},
    ),
    "verifier": ContextProfile(
        agent="verifier",
        role="只读验证明确绑定的最新 Specialist Result",
        system_rules=[
            "不得修改被验证结果",
            "验证必须绑定 handoff_id 和 result_id",
        ],
        allowed_record_types={"result", "verification", "note", "error"},
        retrieval={"max_records": 4},
    ),
}

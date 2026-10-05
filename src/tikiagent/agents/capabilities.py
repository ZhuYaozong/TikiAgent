"""正式应用注册的 Agent 能力；展示给规划器，并检查声明是否匹配。"""

import json

AGENT_CAPABILITIES = {
    "research_agent": {
        "description": "仅通过 Tavily 联网搜索和读取网页；不能读取本地文件、检查解释器或运行命令",
        "capabilities": ["web_research"],
        "tools": {
            "web_search": "输入 query、max_results(1–10)；输出 title/url/snippet/score/published_date，日期可能为 null；不是热度排名服务",
            "web_extract": "输入 url、content_limit；输出实际网页正文（可能截断），可直接提取已知 URL，无须先搜索；提取可能失败",
        },
        "result_contract": "可基于已有证据总结、比较；逐条 finding 引用 source_id，程序回填 URL 和日期；不要要求模型额外输出 URL/长摘录格式",
        "limits": "不能访问浏览器、社交热度/全网排名或自行查询会话存储；可复用显式提供的同 Session 调研结果。缺少日期、热度或正文时如实报告缺口",
    },
    "code_agent": {
        "description": "本地 Workspace 调查、文件修改、Python 环境检查和受权限控制的命令执行",
        "capabilities": ["workspace_read", "workspace_write", "python_environment", "command_execution"],
    },
}


def capability_prompt(available_agents):
    return "实际可委派能力（本地调查交给 CodeAgent）：" + json.dumps(
        {name: AGENT_CAPABILITIES[name] for name in available_agents if name in AGENT_CAPABILITIES},
        ensure_ascii=False, sort_keys=True,
    )


def supports(owner, requirements):
    return set(requirements) <= set(AGENT_CAPABILITIES.get(owner, {}).get("capabilities", []))

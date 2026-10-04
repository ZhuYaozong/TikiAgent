"""正式应用注册的 Agent 能力；展示给规划器，并检查声明是否匹配。"""

import json

AGENT_CAPABILITIES = {
    "research_agent": {
        "description": "仅通过 Tavily 联网搜索和读取网页；不能读取本地文件、检查解释器或运行命令",
        "capabilities": ["web_research"],
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

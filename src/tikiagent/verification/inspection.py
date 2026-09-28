"""只读调查通过独立复读环境证据验收，不要求创建新文件。"""

from tikiagent.orchestration.contracts import CodeResult, VerificationCheck
from tikiagent.tools.models import ToolResult


def verify_inspection(environment, result: CodeResult, execution_context) -> list[VerificationCheck]:
    checks = [VerificationCheck(
        name="inspection_read_only",
        passed=not result.changed_files and not any(
            raw.get("ok") and raw.get("tool_name") in {"write_file", "edit_file", "run_command"}
            for raw in result.tool_results
        ),
        evidence=f"changed_files={result.changed_files}; 检查是否发生写入或命令执行",
    )]
    observations = []
    for raw in result.tool_results:
        try:
            observation = ToolResult.model_validate(raw)
        except ValueError:
            checks.append(VerificationCheck(
                name="inspection_observation_schema", passed=False,
                evidence="调查结果包含不合法的 ToolResult",
            ))
            continue
        if observation.ok and observation.tool_name in {"read_file", "list_files"}:
            observations.append(observation)
    checks.append(VerificationCheck(
        name="inspection_evidence_present", passed=bool(observations),
        evidence=f"可复读的成功观察数量={len(observations)}",
    ))
    for index, observation in enumerate(observations, start=1):
        output = observation.output
        if not isinstance(output, dict) or not isinstance(output.get("path"), str):
            checks.append(VerificationCheck(
                name=f"inspection_evidence:{index}", passed=False,
                evidence="观察缺少结构化输出或真实路径",
            ))
            continue
        path = output["path"]
        replay = environment._execute(
            {"tool_call_id": f"verify_inspection_{index}", "name": observation.tool_name,
             "arguments": {"path": path}},
            execution_context, {observation.tool_name},
        )
        # 老 list_files 输出没有 recursive 字段；独立检查两种合法模式。
        if observation.tool_name == "list_files" and replay.output != output:
            replay = environment._execute(
                {"tool_call_id": f"verify_inspection_recursive_{index}", "name": "list_files",
                 "arguments": {"path": path, "recursive": True}},
                execution_context, {"list_files"},
            )
        checks.append(VerificationCheck(
            name=f"inspection_evidence:{index}",
            passed=replay.ok and replay.output == output,
            evidence=f"tool={observation.tool_name}; path={path}; independently_rechecked={replay.ok and replay.output == output}",
        ))
    return checks

"""不调用模型、不执行命令的基础检查；不冒充语义验收。"""

import json

from tikiagent.harness.workspace import Workspace
from tikiagent.orchestration.contracts import CodeResult, ResearchResult, VerificationCheck, VerificationReport
from tikiagent.tools.models import ToolExecutionError


class BasicResultVerifier:
    """检查交付和机械边界；是否满足用户意图仍由 Supervisor 决定。"""

    def __init__(self, workspace: Workspace):
        self.workspace = workspace

    def verify(self, *, handoff, result, specialist_results):
        del specialist_results
        checks, blockers, limitations = [], [], []

        def check(name, passed, evidence, *, hard=True):
            checks.append(VerificationCheck(name=name, passed=passed, evidence=evidence))
            if not passed:
                (blockers if hard else limitations).append(evidence[:500])

        identity_matches = handoff.result_id == result.result_id and result.handoff_id == handoff.handoff_id
        check("result_identity", identity_matches, "当前 Result/Handoff 身份匹配" if identity_matches else "当前 Result 必须关联已完成 Handoff")
        check("delivery_present", result.delivery_status != "none",
              f"交付状态：{result.delivery_status or 'legacy'}" if result.delivery_status != "none" else "没有实际交付，不能接受为已完成")
        if isinstance(result, ResearchResult):
            pairs = {(o.observation_id, url) for o in result.observations for url in o.urls}
            check("source_provenance", all((s.observation_id, s.url) in pairs for s in result.sources),
                  "来源必须关联真实搜索 Observation；此检查不证明网页内容正确")
            has_evidence = bool(result.findings and result.sources)
            check("research_evidence_present", has_evidence,
                  f"结论 {len(result.findings)} 条，来源 {len(result.sources)} 项" if has_evidence else "调研交付缺少结论或真实来源")
            limitations.extend(f"尚未解决：{q}"[:500] for q in result.unresolved_questions[:12])
        elif isinstance(result, CodeResult):
            if handoff.delivery_mode == "artifact":
                check("artifact_paths_present", bool(result.changed_files),
                      f"交付路径 {len(result.changed_files)} 项" if result.changed_files else "文件交付未提供实际产物路径")
                for path in result.changed_files:
                    try:
                        exists = self.workspace.resolve(path).is_file()
                        check("artifact_exists", exists, f"交付路径 {path[:300]}：{'存在' if exists else '文件不存在'}")
                    except (ToolExecutionError, OSError, ValueError):
                        check("workspace_boundary", False, f"交付文件不在可访问的 Workspace 边界内：{path[:300]}")
            else:
                usable_count = sum(self._usable(r) for r in result.tool_results)
                check("execution_evidence_present", bool(usable_count), f"可用的实际执行记录 {usable_count} 项" if usable_count else
                      "只读/环境交付必须有可用的实际执行记录，不能只依据总结声称完成")
            # 同一命令只保留最后结果；修复后成功的测试不继续携带旧失败。
            commands = {}
            for record in result.tool_results:
                output = record.get("output")
                if isinstance(output, dict) and ("exit_code" in output or "timed_out" in output):
                    key = json.dumps([record.get("tool_name"), output.get("command"), output.get("cwd")], sort_keys=True)
                    commands[key] = record
            for record in commands.values():
                usable = self._usable(record)
                check("command_outcome", usable, "当前命令退出成功且未超时" if usable else
                      "执行记录中仍有未解决的非零退出或超时；不能声称所有测试通过", hard=False)
        if result.delivery_status == "partial":
            limitations.append("Specialist 仅交付部分成果，仍需由 Supervisor 判断未完成内容")
        if isinstance(result, CodeResult) and not result.completed and result.delivery_status != "partial":
            limitations.append("CodeAgent 未声明完成，只能依据已有产物带限制验收")
        if result.stop_reason:
            limitations.append(f"Specialist 停止原因：{result.stop_reason}"[:500])
        if result.finalization_status in {"failed", "already_consumed"}:
            limitations.append("结果整理未完成，不能把已有证据当作完整交付")
        limitations = list(dict.fromkeys(limitations))[:24]
        passed = not blockers
        permission_block = any(c.name == "workspace_boundary" and not c.passed for c in checks)
        retryable = not permission_block and not result.stop_reason and result.delivery_status != "none"
        return VerificationReport(result_id=result.result_id, handoff_id=handoff.handoff_id,
            todo_id=handoff.todo_id, subject_agent=handoff.to_agent, mode="rules", passed=passed,
            checks=checks, failures=blockers, evidence=[c.evidence for c in checks],
            recommendation="仅完成基础检查，未进行独立语义审核；Supervisor 必须结合交付与原始验收条件调用 review_result",
            verification_status="checks_only", verification_reason=handoff.verification_reason,
            hard_blockers=blockers, limitations=limitations, advisory=True,
            failure_category=None if passed else "permission" if permission_block else "validation",
            retryable=None if passed else retryable,
            blocking_reason=None if passed else "; ".join(blockers),
            allowed_actions=[] if passed else ["stop", "replan", "retry"] if retryable else ["stop"])

    @staticmethod
    def _usable(record):
        output = record.get("output")
        return record.get("ok") is True and not (isinstance(output, dict) and (
            output.get("timed_out") or output.get("verified") is False
            or "exit_code" in output and output["exit_code"] != 0))

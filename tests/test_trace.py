"""Trace sequence、脱敏和旁路视图测试。"""

from tikiagent.harness.persistence.trace import JsonlTraceStore


def test_trace_uses_sequence_and_redacts_secrets(tmp_path) -> None:
    trace = JsonlTraceStore(tmp_path / "events.jsonl", max_text_length=8)
    first = trace.append(
        run_id="run-1",
        event_type="approval_requested",
        details={"api_key": "super-secret", "stdout": "x" * 20},
    )
    second = trace.append(
        run_id="run-1",
        event_type="checkpoint_saved",
    )

    assert (first.sequence, second.sequence) == (1, 2)
    assert first.details["api_key"] == "[REDACTED]"
    assert first.details["stdout"].endswith("...[trace truncated]")
    summary, timeline = trace.write_views(tmp_path / "views")
    assert summary.exists()
    assert "approval_requested" in timeline.read_text(encoding="utf-8")

"""收尾错误只保留受控分类，不保存异常正文或供应商凭据。"""
def finalization_error(error):
    details = getattr(error, "diagnostics", {})
    attempts = details.get("attempts", [])
    finish = details.get("finish_reason") or (attempts[-1].get("finish_reason") if attempts else None)
    kind = type(error).__name__
    category = ("output_truncated" if finish == "length" else
                "context_budget" if kind == "ContextBudgetExceeded" else
                "request_budget" if kind == "RequestBudgetExceeded" else
                "invalid_result" if kind in {"ValidationError", "StructuredOutputError", "ValueError", "ModelOutputError"} else
                "provider_error" if kind.endswith(("Error", "Exception")) else "unknown")
    return {"error_category": category, "error_type": kind, "finish_reason": finish}

"""Session analysis aggregation adapted from comain/unit-test-agent `reference/engine/session_analysis.py`."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from cr_agent.review_v2.session_usage import empty_token_bucket, sum_token_bucket


def capture_session_token_usage(
    *,
    state: Dict[str, Any],
    client: Any,
    session_id: Optional[str] = None,
    session_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    target_ids = list(session_ids or [])
    if session_id and session_id not in target_ids:
        target_ids.append(session_id)
    if not target_ids:
        return state.get("session_token_usage", {}) or {}
    aggregated = {
        "session_id": target_ids[-1],
        "session_ids": target_ids,
        "assistant_messages": 0,
        "main_model_tokens": empty_token_bucket(),
        "small_model_tokens": empty_token_bucket(),
        "other_model_tokens": empty_token_bucket(),
        "total_tokens": empty_token_bucket(),
        "by_model": {},
    }
    any_success = False
    for target_id in target_ids:
        try:
            token_usage = client.analyze_session_tokens(target_id)
        except Exception:
            continue
        any_success = True
        aggregated["assistant_messages"] += int(token_usage.get("assistant_messages", 0) or 0)
        for bucket_name in ("main_model_tokens", "small_model_tokens", "other_model_tokens", "total_tokens"):
            sum_token_bucket(aggregated[bucket_name], token_usage.get(bucket_name) or {})
        for model_key, bucket in (token_usage.get("by_model") or {}).items():
            target_bucket = aggregated["by_model"].setdefault(model_key, empty_token_bucket())
            sum_token_bucket(target_bucket, bucket or {})
    return aggregated if any_success else (state.get("session_token_usage", {}) or {})

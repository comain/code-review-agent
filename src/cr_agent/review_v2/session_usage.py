"""OpenCode token bucket helpers adapted from comain/unit-test-agent `reference/engine/session_usage.py`."""

from __future__ import annotations

from typing import Any, Dict, Mapping


TOKEN_METRICS = ("input", "output", "reasoning", "cache_read", "cache_write", "total")
SESSION_TOKEN_BUCKETS = ("main_model_tokens", "small_model_tokens", "other_model_tokens", "total_tokens")


def empty_token_bucket() -> Dict[str, int]:
    return {metric: 0 for metric in TOKEN_METRICS}


def sum_token_bucket(target: Dict[str, int], source: Mapping[str, Any]) -> None:
    for metric in TOKEN_METRICS:
        target[metric] = int(target.get(metric, 0) or 0) + int(source.get(metric, 0) or 0)


def empty_session_token_usage() -> Dict[str, Any]:
    return {bucket: empty_token_bucket() for bucket in SESSION_TOKEN_BUCKETS}


def merge_session_token_usage(dest: Dict[str, Any], src: Mapping[str, Any]) -> None:
    for bucket in SESSION_TOKEN_BUCKETS:
        sum_token_bucket(dest.setdefault(bucket, empty_token_bucket()), src.get(bucket) or {})

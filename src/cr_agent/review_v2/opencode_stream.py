"""Pure JSONL parsing utilities adapted from comain/unit-test-agent `reference/opencode/stream.py`."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional


_RATE_LIMIT_PHRASES = (
    "429",
    "rate limit",
    "rate_limit",
    "ratelimit",
    "quota",
    "too many requests",
    "usage limit",
    "resource_exhausted",
    "free_quota_exhausted",
    "endpoint is inactive",
    "requires more credits",
    "add more credits",
)


class OpenCodeStreamParser:
    def parse_line(self, line: str) -> Optional[Dict[str, Any]]:
        line = line.strip()
        if not line:
            return None
        try:
            loaded = json.loads(line)
        except json.JSONDecodeError:
            return None
        return loaded if isinstance(loaded, dict) else None

    def extract_text(self, events: List[Optional[Dict[str, Any]]]) -> str:
        parts = []
        for event in self._events(events):
            if event.get("type") != "text":
                continue
            part = event.get("part") or {}
            text = part.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
        return "\n".join(parts)

    def extract_session_id(self, events: List[Optional[Dict[str, Any]]]) -> Optional[str]:
        for event in self._events(events):
            value = event.get("sessionID")
            if isinstance(value, str) and value:
                return value
            part = event.get("part") or {}
            value = part.get("sessionID")
            if isinstance(value, str) and value:
                return value
        return None

    def extract_tokens(self, events: List[Optional[Dict[str, Any]]]) -> Dict[str, Any]:
        total: Dict[str, Any] = {
            "input": 0,
            "output": 0,
            "reasoning": 0,
            "cache": {"read": 0, "write": 0},
            "total": 0,
        }
        saw_tokens = False
        for event in self._events(events):
            if event.get("type") != "step_finish":
                continue
            part = event.get("part") or {}
            tokens = part.get("tokens") or {}
            if not isinstance(tokens, dict) or not tokens:
                continue
            saw_tokens = True
            input_tokens = int(tokens.get("input", 0) or 0)
            output_tokens = int(tokens.get("output", 0) or 0)
            reasoning_tokens = int(tokens.get("reasoning", 0) or 0)
            cache = tokens.get("cache") or {}
            cache_read = int(cache.get("read", 0) or 0)
            cache_write = int(cache.get("write", 0) or 0)
            token_total = tokens.get("total")
            if token_total is None:
                token_total = input_tokens + output_tokens + reasoning_tokens + cache_read + cache_write
            total["input"] += input_tokens
            total["output"] += output_tokens
            total["reasoning"] += reasoning_tokens
            total["cache"]["read"] += cache_read
            total["cache"]["write"] += cache_write
            total["total"] += int(token_total or 0)
        return total if saw_tokens else {}

    def extract_cost(self, events: List[Optional[Dict[str, Any]]]) -> Optional[float]:
        total = 0.0
        saw_cost = False
        for event in self._events(events):
            values = [event.get("cost"), (event.get("part") or {}).get("cost")]
            for value in values:
                if value is None:
                    continue
                try:
                    total += float(value)
                    saw_cost = True
                except (TypeError, ValueError):
                    continue
        return total if saw_cost else None

    def count_patches(self, events: List[Optional[Dict[str, Any]]]) -> int:
        count = 0
        for event in self._events(events):
            event_type = event.get("type")
            part = event.get("part") or {}
            if event_type == "patch" or part.get("type") == "patch":
                count += 1
                continue
            if event_type == "tool_use" and part.get("tool") == "apply_patch":
                state = part.get("state") or {}
                output = str(state.get("output") or "")
                if state.get("status") == "completed" and "Success." in output:
                    count += 1
        return count

    def detect_completion(self, events: List[Optional[Dict[str, Any]]]) -> Optional[str]:
        for event in reversed(list(self._events(events))):
            if event.get("type") != "step_finish":
                continue
            reason = (event.get("part") or {}).get("reason")
            if reason == "stop":
                return "stop"
        return None

    def detect_rate_limit(self, error_event: Dict[str, Any]) -> bool:
        if error_event.get("type") != "error":
            return False
        error = error_event.get("error") or {}
        data = error.get("data") or {}
        message = str(data.get("message") or error.get("message") or "").lower()
        status_code = data.get("statusCode") or data.get("status_code")
        if status_code == 429:
            return True
        return any(phrase in message for phrase in _RATE_LIMIT_PHRASES)

    def detect_rate_limit_text(self, text: str) -> bool:
        lowered = (text or "").lower()
        return bool(lowered) and any(phrase in lowered for phrase in _RATE_LIMIT_PHRASES)

    def progress_line(self, event: Dict[str, Any]) -> Optional[str]:
        event_type = event.get("type")
        part = event.get("part") or {}
        if event_type == "text":
            text = str(part.get("text") or "").strip()
            return f"text: {text.splitlines()[0][:180]}" if text else None
        if event_type == "tool_use":
            tool = part.get("tool", "?")
            state = part.get("state") or {}
            return f"tool[{tool}] {state.get('status', 'started')}"
        if event_type == "step_start":
            return "step: started"
        if event_type == "step_finish":
            return f"step: finished ({part.get('reason', 'unknown')})"
        if event_type == "error":
            data = (event.get("error") or {}).get("data") or {}
            message = str(data.get("message") or "unknown error")
            return f"error: {message[:180]}"
        return None

    @staticmethod
    def _events(events: List[Optional[Dict[str, Any]]]) -> List[Dict[str, Any]]:
        return [event for event in events if isinstance(event, dict)]

"""LLM stall recovery helpers adapted from comain/unit-test-agent `reference/engine/llm_session.py`."""

from __future__ import annotations

from typing import Any, Dict, Optional


_STALLED_RECOVERABLE_TYPES = {"stalled_after_recovery", "stalled_no_progress"}
_CONTINUE_PROMPTS = {
    "review": (
        "Resume the interrupted review work in this live session. Do NOT restart broad exploration. "
        "Continue from the current review context and return the required structured review output."
    ),
    "judge": (
        "Resume the interrupted judge work in this live session. Do NOT restart broad exploration. "
        "Continue normalizing the existing reviewer findings for precision, recall, dedupe, and release gating."
    ),
    "feedback": (
        "Resume the interrupted feedback review in this live session. Do NOT restart broad exploration. "
        "Continue from the original finding and latest feedback message."
    ),
}
_DEFAULT_CONTINUE_PROMPT = (
    "Resume the interrupted work in this live session. Do NOT restart broad exploration. "
    "Continue from the current session state and existing context."
)


def continue_prompt_for_phase(phase: str) -> str:
    return _CONTINUE_PROMPTS.get(phase, _DEFAULT_CONTINUE_PROMPT)


def poll_with_continue_recovery(
    *,
    client: Any,
    session_id: str,
    timeout: int,
    phase: str,
    model_id: Optional[str] = None,
    on_update=None,
    stalled_no_progress_seconds: Optional[int] = None,
) -> Dict[str, Any]:
    kwargs = {"timeout": timeout}
    if on_update is not None:
        kwargs["on_update"] = on_update
    if stalled_no_progress_seconds is not None:
        kwargs["stalled_no_progress_seconds"] = stalled_no_progress_seconds
    event = client.poll_completion(session_id, **kwargs)
    if event.get("type") not in _STALLED_RECOVERABLE_TYPES:
        return event
    if on_update:
        on_update(f"recovery: session stalled during {phase}; sending guarded continue prompt")
    client.send_message(session_id, continue_prompt_for_phase(phase), model_id=model_id)
    retry_kwargs = {"timeout": max(120, min(timeout, 600))}
    if on_update is not None:
        retry_kwargs["on_update"] = on_update
    if stalled_no_progress_seconds is not None:
        retry_kwargs["stalled_no_progress_seconds"] = stalled_no_progress_seconds
    return client.poll_completion(session_id, **retry_kwargs)

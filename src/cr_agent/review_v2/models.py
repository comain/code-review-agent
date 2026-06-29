"""CR v2 storage models adapted from comain/unit-test-agent `reference/tasks/models.py`."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import json
from typing import Any, Dict


class ReviewSeverity(str, Enum):
    fatal = "fatal"
    high = "high"
    medium = "medium"
    low = "low"
    info = "info"


TASK_STATUSES = {"queued", "running", "success", "failed", "incomplete", "skipped", "cancelled"}
TERMINAL_TASK_STATUSES = {"success", "failed", "incomplete", "skipped", "cancelled"}
REVIEWER_RUN_STATUSES = {"queued", "running", "success", "failed", "cancelled"}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, sort_keys=True, separators=(",", ":"))


def json_loads(value: Any) -> Dict[str, Any]:
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    try:
        loaded = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def normalize_severity(value: str) -> ReviewSeverity:
    candidate = (value or "").strip().lower()
    if candidate == "critical":
        candidate = "fatal"
    try:
        return ReviewSeverity(candidate)
    except ValueError as exc:
        raise ValueError(f"unsupported severity: {value}") from exc


@dataclass(frozen=True)
class ReviewTaskRow:
    task_id: str
    app_name: str
    repo_url: str
    branch: str
    status: str


@dataclass(frozen=True)
class ReviewerRunRow:
    id: int
    task_id: str
    reviewer: str
    workflow_run_id: str
    attempt: int
    status: str

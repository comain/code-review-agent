"""SQLite-backed DTOs for CR v2 report, recent, and progress surfaces."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import json
from typing import Any, Dict

from cr_agent.review_v2.storage import ReviewDB


class ReviewV2Views:
    def __init__(self, db: ReviewDB):
        self.db = db

    def task_status(self, task_id: str) -> Dict[str, Any]:
        task = self.db.get_task(task_id)
        if task is None:
            return {}
        findings = self._findings(task_id)
        events = self._task_events(task_id)
        reviewer_plan = self._reviewer_plan(task_id)
        review_sessions = self._review_progress(reviewer_plan, self._review_sessions(task_id), events)
        review_mode = self._review_mode(reviewer_plan, events)
        return {
            "task_id": task["task_id"],
            "status": task["status"],
            "gate_status": task["gate_status"],
            "app_name": task["app_name"],
            "branch": task["branch"],
            "report_url": task["report_url"],
            "started_at": task["started_at"],
            "finished_at": task["finished_at"],
            "error_message": task["error"],
            "callback_state": task["callback_state"],
            "review_mode": review_mode,
            "reviewer_plan": reviewer_plan,
            "current_stage": self._current_stage(task, events, review_sessions, review_mode),
            "current_detail": self._current_detail(task, events, review_sessions),
            "review_sessions": review_sessions,
            "events": events,
            "token_usage": self.db.aggregate_task_usage(task_id),
            "result": {
                "summary": task["error"] or ("Review completed" if task["status"] == "success" else "暂无"),
                "score": 100 if not findings else 80,
                "findings": findings,
            },
        }

    def progress_report(self, task_id: str) -> Dict[str, Any]:
        data = self.task_status(task_id)
        if not data:
            return {}
        data["feedback_sessions"] = self._feedback_sessions(task_id)
        return data

    def feedback_progress(self, task_id: str, feedback_session_id: str) -> Dict[str, Any]:
        task = self.db.get_task(task_id)
        if task is None:
            return {}
        sessions = self._feedback_sessions(task_id)
        session = next((item for item in sessions if item["feedback_session_id"] == feedback_session_id), None)
        if session is None:
            return {}
        findings = self._findings(task_id)
        finding = next((item for item in findings if item["finding_id"] == session.get("finding_id")), None)
        events = [
            item
            for item in self._task_events(task_id)
            if (item.get("payload") or {}).get("feedback_session_id") == feedback_session_id
            or (session.get("finding_id") and (item.get("payload") or {}).get("finding_id") == session.get("finding_id"))
        ]
        return {
            "task_id": task_id,
            "feedback_session_id": feedback_session_id,
            "status": session["status"],
            "finding_id": session.get("finding_id"),
            "parent_reviewer_run_id": session.get("parent_reviewer_run_id"),
            "parent_reviewer": session.get("parent_reviewer"),
            "parent_session_id": session.get("parent_session_id"),
            "opencode_session_id": session.get("opencode_session_id"),
            "feedback_text": session.get("feedback_text") or "",
            "model_reply": session.get("model_reply") or "",
            "created_at": session.get("created_at"),
            "updated_at": session.get("updated_at"),
            "finding": finding,
            "task": {
                "status": task["status"],
                "gate_status": task["gate_status"],
                "app_name": task["app_name"],
                "branch": task["branch"],
                "report_url": task["report_url"],
            },
            "token_usage": {
                "input_tokens": session.get("input_tokens") or 0,
                "cache_read_tokens": session.get("cache_read_tokens") or 0,
                "cache_write_tokens": session.get("cache_write_tokens") or 0,
                "output_tokens": session.get("output_tokens") or 0,
                "reasoning_tokens": session.get("reasoning_tokens") or 0,
                "total_tokens": session.get("total_tokens") or 0,
                "cost_usd": session.get("cost_usd") or 0.0,
            },
            "events": events,
        }

    def recent_report(self, *, hours: int, limit: int) -> Dict[str, Any]:
        safe_hours = max(1, hours)
        safe_limit = min(max(1, limit), 1000)
        now = datetime.now(timezone.utc).replace(microsecond=0)
        cutoff_dt = now - timedelta(hours=safe_hours)
        cutoff = cutoff_dt.isoformat()
        with self.db.connect() as conn:
            rows = list(
                conn.execute(
                    """
                    SELECT * FROM cr_tasks
                    WHERE created_at >= ?
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (cutoff, safe_limit),
                )
            )
        tasks = [self._recent_task(dict(row)) for row in rows]
        gate_counts = Counter(item["gate_status"] for item in tasks)
        status_counts = Counter(item["status"] for item in tasks)
        app_counts = Counter(item["app_name"] for item in tasks)
        severity_counts: Counter[str] = Counter()
        for item in tasks:
            severity_counts.update(item["severity_counts"])
        return {
            "generated_at": now.isoformat(),
            "window_start": cutoff_dt.isoformat(),
            "window_end": now.isoformat(),
            "hours": safe_hours,
            "limit": safe_limit,
            "total_matched": len(tasks),
            "total_returned": len(tasks),
            "summary": {
                "total": len(tasks),
                "passed": sum(1 for item in tasks if item["gate_status"] in {"passed", "skipped"}),
                "failed": sum(
                    1
                    for item in tasks
                    if item["status"] == "failed" or item["gate_status"] in {"failed", "incomplete"}
                ),
                "running_or_queued": sum(1 for item in tasks if item["status"] in {"running", "queued"}),
                "findings": sum(item["findings_count"] for item in tasks),
                "callback_succeeded": sum(1 for item in tasks if item["callback_succeeded"]),
                "tokens": sum(item["token_usage"]["total_tokens"] for item in tasks),
                "by_status": dict(sorted(status_counts.items())),
            },
            "status_counts": dict(sorted(status_counts.items())),
            "gate_counts": dict(sorted(gate_counts.items())),
            "app_counts": dict(app_counts.most_common(12)),
            "severity_counts": dict(sorted(severity_counts.items())),
            "tasks": tasks,
        }

    def report_detail(self, task_id: str) -> Dict[str, Any]:
        task = self.db.get_task(task_id)
        if task is None:
            return {}
        findings = self._findings(task_id)
        review_sessions = self._review_sessions(task_id)
        reviewer_plan = self._reviewer_plan(task_id)
        feedback_sessions = self._feedback_sessions(task_id)
        usage = self.db.aggregate_task_usage(task_id)
        request = self._request_json(task)
        return {
            "task_id": task_id,
            "app_name": task["app_name"],
            "repo_url": task["repo_url"],
            "branch": task["branch"],
            "commit_id": task["commit_id"],
            "status": task["status"],
            "summary": task["error"] or "Review completed",
            "score": 100 if not findings else 80,
            "pass_check": task["gate_status"] in {"passed", "skipped"},
            "report_url": task["report_url"],
            "gate_status": task["gate_status"],
            "created_at": task["created_at"],
            "started_at": task["started_at"],
            "finished_at": task["finished_at"],
            "duration_seconds": self._duration_seconds(task),
            "callback_state": task["callback_state"],
            "request": request,
            "trigger_source": self._trigger_source(request),
            "findings": findings,
            "findings_count": len(findings),
            "severity_counts": dict(sorted(Counter(item["severity"] for item in findings).items())),
            "review_mode": self._review_mode(reviewer_plan, self._task_events(task_id)),
            "reviewer_plan": reviewer_plan,
            "review_sessions": review_sessions,
            "feedback_sessions": feedback_sessions,
            "review_session_count": len(review_sessions),
            "feedback_session_count": len(feedback_sessions),
            "events": self._task_events(task_id),
            "token_usage": usage,
        }

    def _recent_task(self, task: Dict[str, Any]) -> Dict[str, Any]:
        findings = self._findings(task["task_id"])
        usage = self.db.aggregate_task_usage(task["task_id"])
        usage["calls"] = len(self._review_sessions(task["task_id"]))
        usage["source"] = "sqlite"
        return {
            "task_id": task["task_id"],
            "app_name": task["app_name"],
            "branch": task["branch"],
            "commit_id": task.get("commit_id"),
            "status": task["status"],
            "gate_status": task.get("gate_status") or task["status"],
            "attempts": task.get("attempts") or 0,
            "findings_count": len(findings),
            "severity_counts": dict(sorted(Counter(item["severity"] for item in findings).items())),
            "score": 100 if not findings else 80,
            "summary": task.get("error") or "Review completed",
            "report_url": task.get("report_url"),
            "callback_succeeded": task.get("callback_state") == "succeeded",
            "created_at": task.get("created_at"),
            "duration_seconds": None,
            "token_usage": usage,
            "ci_task_id": None,
            "ci_record_id": None,
        }

    def _findings(self, task_id: str) -> list[Dict[str, Any]]:
        with self.db.connect() as conn:
            return [
                {
                    "finding_id": row["finding_id"],
                    "file": row["file_path"],
                    "line": row["line"],
                    "severity": row["severity"],
                    "title": row["title"],
                    "detail": row["detail"],
                    "suggestion": row["suggestion"],
                    "status": row["status"],
                    "source_reviewer_run_id": row["source_reviewer_run_id"],
                    "source_reviewer": row["source_reviewer"],
                    "source_session_id": row["source_session_id"],
                }
                for row in conn.execute(
                    """
                    SELECT
                        f.*,
                        f.reviewer_run_id AS source_reviewer_run_id,
                        rr.reviewer AS source_reviewer,
                        rr.session_id AS source_session_id
                    FROM findings f
                    LEFT JOIN reviewer_runs rr ON rr.id = f.reviewer_run_id
                    WHERE f.task_id=?
                    ORDER BY f.created_at, f.finding_id
                    """,
                    (task_id,),
                )
            ]

    def _review_sessions(self, task_id: str) -> list[Dict[str, Any]]:
        with self.db.connect() as conn:
            return [
                {
                    "id": row["id"],
                    "reviewer": row["reviewer"],
                    "model_id": row["model_id"],
                    "status": row["status"],
                    "session_id": row["session_id"],
                    "started_at": row["started_at"],
                    "finished_at": row["finished_at"],
                    "duration_seconds": row["duration_seconds"],
                    "total_tokens": row["total_tokens"],
                    "cost_usd": row["cost_usd"],
                    "error": row["error"],
                }
                for row in conn.execute("SELECT * FROM reviewer_runs WHERE task_id=? ORDER BY created_at, id", (task_id,))
            ]

    def _reviewer_plan(self, task_id: str) -> list[Dict[str, Any]]:
        with self.db.connect() as conn:
            return [
                {
                    "reviewer": row["reviewer"],
                    "required": bool(row["required"]),
                    "risk_tier": row["risk_tier"],
                    "reason": row["reason"],
                    "created_at": row["created_at"],
                }
                for row in conn.execute(
                    "SELECT * FROM reviewer_plans WHERE task_id=? ORDER BY created_at, id",
                    (task_id,),
                )
            ]

    @staticmethod
    def _review_progress(plans: list[Dict[str, Any]], runs: list[Dict[str, Any]], events: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
        latest_run_by_reviewer: dict[str, Dict[str, Any]] = {}
        for run in runs:
            latest_run_by_reviewer[run["reviewer"]] = run
        merged: list[Dict[str, Any]] = []
        planned_reviewers = set()
        for plan in plans:
            reviewer = plan["reviewer"]
            planned_reviewers.add(reviewer)
            run = latest_run_by_reviewer.get(reviewer)
            if run is not None:
                merged.append({**plan, **run, "required": plan["required"], "risk_tier": plan["risk_tier"], "reason": plan["reason"]})
                continue
            merged.append(
                {
                    **plan,
                    "status": "queued",
                    "model_id": None,
                    "session_id": None,
                    "started_at": None,
                    "finished_at": None,
                    "duration_seconds": None,
                    "total_tokens": 0,
                    "cost_usd": 0.0,
                    "error": None,
                }
            )
        merged.extend(run for run in runs if run["reviewer"] not in planned_reviewers)
        if not any(item["reviewer"] == "cr_judge" for item in merged):
            judge = ReviewV2Views._synthetic_judge_session(events)
            if judge is not None:
                merged.append(judge)
        return merged

    @staticmethod
    def _synthetic_judge_session(events: list[Dict[str, Any]]) -> Dict[str, Any] | None:
        judge_events = [item for item in events if str(item.get("event_type") or "").startswith("judge_")]
        if not judge_events:
            return None
        status = "running"
        if any(item["event_type"] == "judge_failed" for item in judge_events):
            status = "failed"
        elif any(item["event_type"] == "judge_completed" for item in judge_events):
            status = "success"
        started = next((item for item in judge_events if item["event_type"] == "judge_started"), judge_events[0])
        finished = next((item for item in reversed(judge_events) if item["event_type"] in {"judge_completed", "judge_failed"}), None)
        return {
            "reviewer": "cr_judge",
            "required": True,
            "risk_tier": None,
            "reason": "final finding judge",
            "created_at": started.get("created_at"),
            "id": None,
            "model_id": None,
            "status": status,
            "session_id": None,
            "started_at": started.get("created_at"),
            "finished_at": finished.get("created_at") if finished else None,
            "duration_seconds": None,
            "total_tokens": 0,
            "cost_usd": 0.0,
            "error": finished.get("message") if finished and status == "failed" else None,
        }

    def _feedback_sessions(self, task_id: str) -> list[Dict[str, Any]]:
        with self.db.connect() as conn:
            return [
                {
                    "feedback_session_id": row["feedback_session_id"],
                    "task_id": row["task_id"],
                    "finding_id": row["finding_id"],
                    "parent_reviewer_run_id": row["parent_reviewer_run_id"],
                    "parent_reviewer": row["parent_reviewer"],
                    "parent_session_id": row["parent_session_id"],
                    "status": row["status"],
                    "opencode_session_id": row["opencode_session_id"],
                    "feedback_text": row["feedback_text"],
                    "model_reply": self._row_value(row, "model_reply"),
                    "input_tokens": row["input_tokens"],
                    "cache_read_tokens": row["cache_read_tokens"],
                    "cache_write_tokens": row["cache_write_tokens"],
                    "output_tokens": row["output_tokens"],
                    "reasoning_tokens": row["reasoning_tokens"],
                    "total_tokens": row["total_tokens"],
                    "cost_usd": row["cost_usd"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
                for row in conn.execute(
                    """
                    SELECT
                        fs.*,
                        rr.reviewer AS parent_reviewer,
                        rr.session_id AS parent_session_id
                    FROM feedback_sessions fs
                    LEFT JOIN reviewer_runs rr ON rr.id = fs.parent_reviewer_run_id
                    WHERE fs.task_id=?
                    ORDER BY fs.created_at
                    """,
                    (task_id,),
                )
            ]

    @staticmethod
    def _row_value(row: Any, key: str, default: Any = None) -> Any:
        return row[key] if key in row.keys() else default

    def _task_events(self, task_id: str) -> list[Dict[str, Any]]:
        with self.db.connect() as conn:
            return [
                {
                    "event_type": row["event_type"],
                    "severity": row["severity"],
                    "stage": row["stage"],
                    "message": row["message"],
                    "payload": self._parse_json(row["payload_json"]),
                    "created_at": row["created_at"],
                }
                for row in conn.execute("SELECT * FROM task_events WHERE task_id=? ORDER BY created_at, id", (task_id,))
            ]

    @staticmethod
    def _request_json(task: Any) -> Dict[str, Any]:
        try:
            payload = json.loads(task["request_json"] or "{}")
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _parse_json(value: Any) -> Dict[str, Any]:
        try:
            payload = json.loads(value or "{}")
        except (TypeError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _review_mode(reviewer_plan: list[Dict[str, Any]], events: list[Dict[str, Any]]) -> str:
        for item in reviewer_plan:
            if item.get("risk_tier"):
                return str(item["risk_tier"])
        for event in reversed(events):
            payload = event.get("payload") or {}
            if payload.get("risk_tier"):
                return str(payload["risk_tier"])
        return "pending"

    @staticmethod
    def _trigger_source(request: Dict[str, Any]) -> str:
        if request.get("ci_task_id") or request.get("ci_record_id"):
            return "ci"
        return str(request.get("source") or request.get("trigger_source") or "api")

    @staticmethod
    def _duration_seconds(task: Any) -> float | None:
        if not task["started_at"] or not task["finished_at"]:
            return None
        try:
            return (datetime.fromisoformat(task["finished_at"]) - datetime.fromisoformat(task["started_at"])).total_seconds()
        except ValueError:
            return None

    @staticmethod
    def _current_stage(
        task: Any,
        events: list[Dict[str, Any]],
        review_sessions: list[Dict[str, Any]],
        review_mode: str,
    ) -> str:
        running_items = [item for item in review_sessions if item["status"] == "running"]
        if running_items and len(review_sessions) > 1:
            return f"reviewer_fanout:{review_mode}"
        running = running_items[0] if running_items else None
        if running:
            return f"reviewer:{running['reviewer']}"
        if task["status"] == "running" and any(item["status"] == "queued" for item in review_sessions):
            return f"reviewer_fanout:{review_mode}"
        if task["status"] in {"queued", "running"} and events:
            return events[-1].get("stage") or task["status"]
        return task["gate_status"] or task["status"]

    @staticmethod
    def _current_detail(task: Any, events: list[Dict[str, Any]], review_sessions: list[Dict[str, Any]]) -> str:
        running_items = [item for item in review_sessions if item["status"] == "running"]
        queued_items = [item for item in review_sessions if item["status"] == "queued"]
        if running_items:
            running = ", ".join(item["reviewer"] for item in running_items)
            queued = f"; queued: {', '.join(item['reviewer'] for item in queued_items)}" if queued_items else ""
            return f"{len(running_items)} reviewer(s) running: {running}{queued}"
        if task["status"] == "running" and queued_items:
            return f"waiting for reviewer fanout: {', '.join(item['reviewer'] for item in queued_items)}"
        if events:
            return events[-1].get("message") or ""
        return task["error"] or ""

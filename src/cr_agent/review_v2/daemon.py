"""CR v2 daemon adapted from comain/unit-test-agent `reference/tasks/scheduler.py`."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
import os
import socket
import time
from typing import Any, Optional
from zoneinfo import ZoneInfo

from cr_agent.config import Settings
from cr_agent.core.callback import CallbackClient
from cr_agent.models import CallbackPayload, TaskRecord, TaskStatus, TriggerRequest
from cr_agent.review_v2.feedback_patterns import sync_feedback_patterns_from_db
from cr_agent.review_v2.models import now_iso
from cr_agent.review_v2.storage import ReviewDB
from cr_agent.review_v2.workflow import WorkflowRunner

logger = logging.getLogger(__name__)


class CRReviewDaemon:
    def __init__(
        self,
        settings: Settings,
        *,
        db: Optional[ReviewDB] = None,
        workflow_runner=None,
        callback_client: Optional[CallbackClient] = None,
        daemon_id: Optional[str] = None,
        feedback_pattern_syncer=None,
    ):
        self.settings = settings
        self.db = db or ReviewDB(settings.review_v2_db_path)
        self.db.init()
        self.daemon_id = daemon_id or settings.review_v2_daemon_id or f"{socket.gethostname()}:{os.getpid()}"
        self.workflow_runner = workflow_runner or WorkflowRunner(settings, self.db)
        self.callback_client = callback_client or CallbackClient(settings)
        self.feedback_pattern_syncer = feedback_pattern_syncer or sync_feedback_patterns_from_db
        self._last_feedback_pattern_sync_date: Optional[str] = None

    def once(self) -> int:
        claimed = self.db.claim_next_task(
            daemon_id=self.daemon_id,
            lease_seconds=self.settings.review_v2_daemon_lease_seconds,
        )
        if claimed is None:
            self.db.upsert_heartbeat(
                runner_id=self.daemon_id,
                task_id=None,
                status="IDLE",
                message="no queued task",
                pid=os.getpid(),
                hostname=socket.gethostname(),
            )
            return 0
        task_id = claimed["task_id"]
        self.db.upsert_heartbeat(
            runner_id=self.daemon_id,
            task_id=task_id,
            status="RUNNING",
            message="task acquired",
            pid=os.getpid(),
            hostname=socket.gethostname(),
        )
        self.workflow_runner.run(task_id)
        return 1

    def run_forever(self, *, poll_interval: Optional[float] = None, once: bool = False) -> None:
        interval = self.settings.review_v2_daemon_poll_interval_seconds if poll_interval is None else poll_interval
        while True:
            self.maybe_sync_feedback_patterns()
            self.recover_stale()
            self.retry_callbacks()
            self.once()
            if once:
                return
            time.sleep(max(0.1, float(interval)))

    def maybe_sync_feedback_patterns(self, *, now: Optional[datetime] = None) -> bool:
        if not self.settings.review_v2_feedback_pattern_sync_enabled:
            return False
        now_local = (now or datetime.now(ZoneInfo("Asia/Shanghai"))).astimezone(ZoneInfo("Asia/Shanghai"))
        schedule_minutes = (
            int(self.settings.review_v2_feedback_pattern_sync_hour) * 60
            + int(self.settings.review_v2_feedback_pattern_sync_minute)
        )
        current_minutes = now_local.hour * 60 + now_local.minute
        sync_date = now_local.date().isoformat()
        if current_minutes < schedule_minutes or self._last_feedback_pattern_sync_date == sync_date:
            return False
        self._last_feedback_pattern_sync_date = sync_date
        try:
            result = self.feedback_pattern_syncer(self.db, self.settings)
            payload = result.to_dict() if hasattr(result, "to_dict") else dict(result or {})
            self.db.add_task_event(
                None,
                "feedback_pattern_sync_completed",
                "feedback pattern sync completed",
                stage="feedback_pattern_sync",
                payload=payload,
            )
            self.db.upsert_heartbeat(
                runner_id=self.daemon_id,
                task_id=None,
                status="PATTERN_SYNC",
                message=f"feedback patterns total={payload.get('total_count', 0)} changed={payload.get('changed', False)}",
                pid=os.getpid(),
                hostname=socket.gethostname(),
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.exception("feedback pattern sync failed")
            self.db.add_task_event(
                None,
                "feedback_pattern_sync_failed",
                "feedback pattern sync failed",
                stage="feedback_pattern_sync",
                severity="error",
                payload={"error": str(exc)},
            )
            return False

    def recover_stale(self) -> list[str]:
        recovered = self.db.recover_stale_running_tasks(max_attempts=self.settings.max_task_attempts)
        self.db.upsert_heartbeat(
            runner_id=self.daemon_id,
            task_id=None,
            status="RECOVER",
            message=f"recovered {len(recovered)} stale tasks",
            pid=os.getpid(),
            hostname=socket.gethostname(),
        )
        return recovered

    def retry_callbacks(self) -> list[str]:
        due = self.db.due_callback_tasks(limit=self.settings.review_v2_daemon_claim_limit)
        completed: list[str] = []
        for task_id in due:
            if self._send_callback(task_id):
                completed.append(task_id)
        self.db.upsert_heartbeat(
            runner_id=self.daemon_id,
            task_id=None,
            status="CALLBACK_RETRY",
            message=f"due callbacks {len(due)}, completed {len(completed)}",
            pid=os.getpid(),
            hostname=socket.gethostname(),
        )
        return completed

    def status_payload(self) -> dict:
        return {
            "counts": self.db.task_counts(),
            "heartbeats": [dict(row) for row in self.db.latest_heartbeats()],
            "active_tasks": [dict(row) for row in self.db.active_tasks()],
        }

    def _send_callback(self, task_id: str) -> bool:
        task = self.db.get_task(task_id)
        if task is None:
            return False
        try:
            record = self._task_record_for_callback(task)
            if not self._has_callback_target(record.request):
                self.db.set_callback_state(task_id, state="skipped", attempts=int(task["callback_attempts"] or 0))
                return True
            payload = self._callback_payload(task, record)
            payload_digest = self._payload_digest(payload)
            attempts = int(task["callback_attempts"] or 0)
            if task["callback_state"] == "succeeded" and task["callback_payload_digest"] == payload_digest:
                return True
            history = self.callback_client.send(record, payload)
            self.db.set_callback_state(
                task_id,
                state="succeeded",
                attempts=attempts + 1,
                payload_digest=payload_digest,
                history=history,
            )
            self.db.add_task_event(
                task_id,
                "callback_succeeded",
                "callback acknowledged",
                stage="callback",
                payload={"attempts": attempts + 1},
            )
            return True
        except Exception as exc:  # noqa: BLE001
            self._record_callback_failure(task, str(exc))
            return False

    def _record_callback_failure(self, task: Any, error: str) -> None:
        attempts = int(task["callback_attempts"] or 0) + 1
        if attempts >= self.settings.review_v2_callback_max_attempts:
            self.db.set_callback_state(task["task_id"], state="failed", attempts=attempts, last_error=error)
            self.db.add_task_event(
                task["task_id"],
                "callback_failed",
                "callback retry budget exhausted",
                stage="callback",
                severity="error",
                payload={"attempts": attempts, "error": error},
            )
            return
        delay = min(
            self.settings.review_v2_callback_initial_delay_seconds * (2 ** max(0, attempts - 1)),
            self.settings.review_v2_callback_max_delay_seconds,
        )
        next_retry_at = (datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=delay)).isoformat()
        self.db.set_callback_state(
            task["task_id"],
            state="retrying",
            attempts=attempts,
            next_retry_at=next_retry_at,
            last_error=error,
        )
        self.db.add_task_event(
            task["task_id"],
            "callback_retry_scheduled",
            "callback failed; retry scheduled",
            stage="callback",
            severity="warn",
            payload={"attempts": attempts, "next_retry_at": next_retry_at, "error": error},
        )

    def _task_record_for_callback(self, task: Any) -> TaskRecord:
        request_payload = json.loads(task["request_json"] or "{}")
        request_data = {
            "app_name": task["app_name"],
            "repo_url": task["repo_url"],
            "branch": task["branch"],
            **request_payload,
        }
        try:
            request = TriggerRequest.model_validate(request_data)
        except ValueError:
            request = TriggerRequest.model_construct(
                app_name=str(request_data.get("app_name") or ""),
                repo_url=str(request_data.get("repo_url") or ""),
                branch=str(request_data.get("branch") or ""),
                commit_id=request_data.get("commit_id"),
                callback_url=request_data.get("callback_url"),
                callback_token=request_data.get("callback_token"),
                pipeline_id=request_data.get("pipeline_id"),
                sprint_id=request_data.get("sprint_id"),
                operator=request_data.get("operator"),
                trigger_source=str(request_data.get("trigger_source") or "manual"),
                metadata=dict(request_data.get("metadata") or {}),
                ci_task_id=request_data.get("ci_task_id"),
                ci_record_id=request_data.get("ci_record_id"),
                ci_parent_id=request_data.get("ci_parent_id"),
                ci_task_template_id=request_data.get("ci_task_template_id"),
            )
        return TaskRecord(
            task_id=task["task_id"],
            status=TaskStatus.success if self._task_passed(task) else TaskStatus.failed,
            request=request,
            report_url=task["report_url"] or self._default_report_url(task["task_id"]),
        )

    def _callback_payload(self, task: Any, record: TaskRecord) -> CallbackPayload:
        findings_count = self._findings_count(task["task_id"])
        passed = self._task_passed(task)
        return CallbackPayload(
            task_id=task["task_id"],
            app_name=task["app_name"],
            branch=task["branch"],
            commit_id=task["commit_id"],
            passed=passed,
            score=100 if passed else 80,
            report_url=record.report_url or self._default_report_url(task["task_id"]),
            status=record.status,
            summary=task["error"] or ("Review passed." if passed else "Review found issues."),
            findings_count=findings_count,
            generated_at=datetime.fromisoformat(now_iso()),
            metadata=record.request.metadata,
        )

    def _findings_count(self, task_id: str) -> int:
        with self.db.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM findings WHERE task_id=?", (task_id,)).fetchone()
        return int(row["n"] or 0)

    def _default_report_url(self, task_id: str) -> str:
        return f"{self.settings.report_base_url.rstrip('/')}/{task_id}/index.html"

    @staticmethod
    def _task_passed(task: Any) -> bool:
        return task["gate_status"] in {"passed", "skipped"}

    @staticmethod
    def _has_callback_target(request: TriggerRequest) -> bool:
        return bool(request.callback_url or (request.ci_task_id and request.ci_record_id))

    @staticmethod
    def _payload_digest(payload: CallbackPayload) -> str:
        body = payload.model_dump(mode="json")
        body.pop("generated_at", None)
        return json.dumps(body, ensure_ascii=False, sort_keys=True)

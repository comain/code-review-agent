from __future__ import annotations

import json
import logging
import queue
import re
import threading
import uuid
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from collections import Counter
from pathlib import Path
import subprocess
from typing import Any, Dict, List, Optional

from cr_agent.config import Settings
from cr_agent.core.callback import CallbackClient
from cr_agent.core.feedback import FeedbackStore
from cr_agent.core.git_client import GitClient
from cr_agent.core.opencode_runner import OpencodeRunner
from cr_agent.core.reporting import ReportWriter
from cr_agent.core.storage import TaskStore
from cr_agent.core.usage import UsageStore
from cr_agent.models import CallbackPayload, FeedbackMessage, FeedbackRole, FindingFeedbackThread, FixSession
from cr_agent.models import FixSessionStage, FixSessionView, GeneralFeedbackItem
from cr_agent.models import FindingStatus, FindingView, ReportDetail, TaskRecord, TaskStatus, TriggerRequest
from cr_agent.models import AnalysisResult, Finding, FindingSeverity
from cr_agent.review_v2.storage import ReviewDB

logger = logging.getLogger(__name__)


class TaskService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = TaskStore(settings.task_dir)
        self.git = GitClient(settings)
        self.runner = OpencodeRunner(settings)
        self.reporter = ReportWriter(settings)
        self.feedback = FeedbackStore(settings.report_dir, settings.issues_dir)
        self.usage = UsageStore(settings.usage_dir)
        self.callback = CallbackClient(settings)
        self.review_v2_db = ReviewDB(settings.review_v2_db_path)
        if settings.review_v2_enabled:
            self.review_v2_db.init()
        self._queue: queue.Queue[str] = queue.Queue(maxsize=settings.queue_size)
        self._feedback_queue: queue.Queue[tuple[str, int]] = queue.Queue(maxsize=settings.queue_size * 4)
        self._general_feedback_queue: queue.Queue[tuple[str, str]] = queue.Queue(maxsize=settings.queue_size * 4)
        self._fix_queue: queue.Queue[tuple[str, str]] = queue.Queue(maxsize=settings.queue_size * 4)
        self._executor = ThreadPoolExecutor(
            max_workers=settings.worker_threads,
            thread_name_prefix="cr-agent-worker",
        )
        self._started = False
        self._lock = threading.Lock()
        self._submit_lock = threading.Lock()
        self._active_tasks: set[str] = set()
        self._active_lock = threading.Lock()
        self._cancelled_tasks: set[str] = set()
        self._deleted_tasks: set[str] = set()
        self._recovery_thread: Optional[threading.Thread] = None
        self._feedback_thread: Optional[threading.Thread] = None
        self._general_feedback_thread: Optional[threading.Thread] = None
        self._fix_threads: List[threading.Thread] = []

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            for _ in range(self.settings.worker_threads):
                self._executor.submit(self._worker_loop)
            self._recover_pending_tasks()
            self._recovery_thread = threading.Thread(
                target=self._recovery_loop,
                name="cr-agent-recovery",
                daemon=True,
            )
            self._recovery_thread.start()
            self._feedback_thread = threading.Thread(
                target=self._feedback_loop,
                name="cr-agent-feedback",
                daemon=True,
            )
            self._feedback_thread.start()
            self._general_feedback_thread = threading.Thread(
                target=self._general_feedback_loop,
                name="cr-agent-general-feedback",
                daemon=True,
            )
            self._general_feedback_thread.start()
            for index in range(max(1, self.settings.fix_worker_threads)):
                thread = threading.Thread(
                    target=self._fix_loop,
                    name=f"cr-agent-fix-{index + 1}",
                    daemon=True,
                )
                thread.start()
                self._fix_threads.append(thread)
            self._started = True

    def submit(self, request: TriggerRequest) -> TaskRecord:
        if self.settings.review_v2_enabled:
            return self._submit_v2(request)
        with self._submit_lock:
            existing_by_commit = self._find_same_commit_record(request)
            if existing_by_commit is not None:
                logger.info(
                    "task=%s submit idempotent-hit app=%s branch=%s repo=%s commit=%s status=%s",
                    existing_by_commit.task_id,
                    request.app_name,
                    request.branch,
                    request.repo_url,
                    request.commit_id,
                    existing_by_commit.status,
                )
                return existing_by_commit

            existing = self._find_inflight_duplicate(request)
            if existing is not None:
                logger.info(
                    "task=%s submit deduplicated app=%s branch=%s repo=%s status=%s",
                    existing.task_id,
                    request.app_name,
                    request.branch,
                    request.repo_url,
                    existing.status,
                )
                return existing

            task_id = uuid.uuid4().hex
            record = TaskRecord(task_id=task_id, status=TaskStatus.queued, request=request)
            self._save_record(record)
            self._enqueue_task(task_id)
            logger.info(
                "task=%s submitted app=%s branch=%s repo=%s status=%s",
                task_id,
                request.app_name,
                request.branch,
                request.repo_url,
                record.status,
            )
            return record

    def _submit_v2(self, request: TriggerRequest) -> TaskRecord:
        with self._submit_lock:
            self.review_v2_db.init()
            task_id = uuid.uuid4().hex
            report_url = f"{self.settings.report_base_url.rstrip('/')}/{task_id}/index.html"
            self.review_v2_db.create_task(
                task_id=task_id,
                app_name=request.app_name,
                repo_url=request.repo_url,
                branch=request.branch,
                commit_id=request.commit_id,
                priority=100,
                request_json=request.model_dump(mode="json"),
            )
            record = TaskRecord(
                task_id=task_id,
                status=TaskStatus.queued,
                request=request,
                report_url=report_url,
            )
            logger.info(
                "task=%s submitted to review_v2 sqlite queue app=%s branch=%s repo=%s",
                task_id,
                request.app_name,
                request.branch,
                request.repo_url,
            )
            return record

    def get(self, task_id: str) -> Optional[TaskRecord]:
        return self.store.get(task_id)

    def list_all(self) -> List[TaskRecord]:
        return self.store.list_all()

    def list_running_tasks(self) -> List[TaskRecord]:
        return [record for record in self.list_all() if record.status == TaskStatus.running]

    def get_dashboard_metrics(
        self,
        start_at: Optional[datetime] = None,
        end_at: Optional[datetime] = None,
        bucket_minutes: int = 60,
    ) -> Dict[str, List[Dict[str, Any]]]:
        bucket_seconds = max(bucket_minutes, 1) * 60
        count_buckets: Dict[int, int] = {}
        duration_buckets: Dict[int, List[float]] = {}

        for record in self.list_all():
            created_at = record.created_at
            if start_at and created_at < start_at:
                continue
            if end_at and created_at > end_at:
                continue

            created_bucket = int(created_at.timestamp() // bucket_seconds * bucket_seconds)
            count_buckets[created_bucket] = count_buckets.get(created_bucket, 0) + 1

            if record.started_at and record.finished_at:
                finished_at = record.finished_at
                if start_at and finished_at < start_at:
                    continue
                if end_at and finished_at > end_at:
                    continue
                duration = (record.finished_at - record.started_at).total_seconds()
                finished_bucket = int(finished_at.timestamp() // bucket_seconds * bucket_seconds)
                duration_buckets.setdefault(finished_bucket, []).append(duration)

        task_counts = [
            {"time": self._bucket_iso(bucket), "count": count_buckets[bucket]}
            for bucket in sorted(count_buckets)
        ]
        duration_p99 = [
            {"time": self._bucket_iso(bucket), "p99_seconds": self._percentile(duration_buckets[bucket], 0.99)}
            for bucket in sorted(duration_buckets)
        ]
        return {"task_counts": task_counts, "duration_p99": duration_p99}

    def get_cost_report(
        self,
        start_at: Optional[datetime] = None,
        end_at: Optional[datetime] = None,
        bucket_minutes: int = 60,
    ) -> Dict[str, Any]:
        bucket_seconds = max(bucket_minutes, 1) * 60
        rows = []
        for item in self.usage.list_all():
            created_at = item["created_at"]
            if start_at and created_at < start_at:
                continue
            if end_at and created_at > end_at:
                continue
            rows.append(item)

        total_calls = len(rows)
        total_tokens = sum(int(item.get("total_tokens") or 0) for item in rows)
        total_prompt_tokens = sum(int(item.get("prompt_tokens") or 0) for item in rows)
        total_completion_tokens = sum(int(item.get("completion_tokens") or 0) for item in rows)
        total_thinking_tokens = sum(int(item.get("thinking_tokens") or 0) for item in rows)
        avg_tokens_per_call = round(total_tokens / total_calls, 1) if total_calls else 0

        category_totals: Dict[str, Dict[str, float]] = {}
        time_buckets: Dict[int, Dict[str, int]] = {}
        for item in rows:
            category = item.get("category") or "unknown"
            stats = category_totals.setdefault(
                category,
                {"calls": 0, "tokens": 0, "prompt_tokens": 0, "completion_tokens": 0, "thinking_tokens": 0},
            )
            stats["calls"] += 1
            stats["tokens"] += int(item.get("total_tokens") or 0)
            stats["prompt_tokens"] += int(item.get("prompt_tokens") or 0)
            stats["completion_tokens"] += int(item.get("completion_tokens") or 0)
            stats["thinking_tokens"] += int(item.get("thinking_tokens") or 0)

            bucket = int(item["created_at"].timestamp() // bucket_seconds * bucket_seconds)
            bucket_stats = time_buckets.setdefault(
                bucket,
                {"tokens": 0, "prompt_tokens": 0, "completion_tokens": 0, "thinking_tokens": 0},
            )
            bucket_stats["tokens"] += int(item.get("total_tokens") or 0)
            bucket_stats["prompt_tokens"] += int(item.get("prompt_tokens") or 0)
            bucket_stats["completion_tokens"] += int(item.get("completion_tokens") or 0)
            bucket_stats["thinking_tokens"] += int(item.get("thinking_tokens") or 0)

        avg_tokens_by_category = [
            {
                "category": category,
                "avg_tokens": round(stats["tokens"] / stats["calls"], 1) if stats["calls"] else 0,
                "avg_prompt_tokens": round(stats["prompt_tokens"] / stats["calls"], 1) if stats["calls"] else 0,
                "avg_completion_tokens": round(stats["completion_tokens"] / stats["calls"], 1) if stats["calls"] else 0,
                "avg_thinking_tokens": round(stats["thinking_tokens"] / stats["calls"], 1) if stats["calls"] else 0,
                "calls": int(stats["calls"]),
                "total_tokens": int(stats["tokens"]),
            }
            for category, stats in sorted(category_totals.items())
        ]
        tokens_over_time = [
            {
                "time": self._bucket_iso(bucket),
                "tokens": time_buckets[bucket]["tokens"],
                "prompt_tokens": time_buckets[bucket]["prompt_tokens"],
                "completion_tokens": time_buckets[bucket]["completion_tokens"],
                "thinking_tokens": time_buckets[bucket]["thinking_tokens"],
            }
            for bucket in sorted(time_buckets)
        ]
        return {
            "summary": {
                "total_calls": total_calls,
                "total_tokens": total_tokens,
                "total_prompt_tokens": total_prompt_tokens,
                "total_completion_tokens": total_completion_tokens,
                "total_thinking_tokens": total_thinking_tokens,
                "avg_tokens_per_call": avg_tokens_per_call,
            },
            "avg_tokens_by_category": avg_tokens_by_category,
            "tokens_over_time": tokens_over_time,
        }

    def delete_running_tasks(
        self,
        start_at: Optional[datetime] = None,
        end_at: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        deleted_ids: List[str] = []
        for record in self.list_running_tasks():
            reference_time = record.started_at or record.created_at
            if start_at and reference_time < start_at:
                continue
            if end_at and reference_time > end_at:
                continue
            self._cancel_task(record.task_id)
            deleted_ids.append(record.task_id)
        return {"deleted_count": len(deleted_ids), "task_ids": deleted_ids}

    def list_tasks_page(
        self,
        start_at: Optional[datetime] = None,
        end_at: Optional[datetime] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        records = []
        for record in self.list_all():
            created_at = record.created_at
            if start_at and created_at < start_at:
                continue
            if end_at and created_at > end_at:
                continue
            records.append(record)
        records.sort(key=lambda item: item.created_at, reverse=True)
        total = len(records)
        total_pages = max(1, (total + page_size - 1) // page_size) if page_size > 0 else 1
        safe_page = min(max(page, 1), total_pages)
        safe_page_size = max(page_size, 1)
        start_index = (safe_page - 1) * safe_page_size
        end_index = start_index + safe_page_size
        page_items = records[start_index:end_index]
        return {
            "total": total,
            "page": safe_page,
            "page_size": safe_page_size,
            "items": [
                {
                    "task_id": record.task_id,
                    "app_name": record.request.app_name,
                    "branch": record.request.branch,
                    "created_at": record.created_at.isoformat(),
                    "status": record.status.value,
                    "report_url": record.report_url,
                }
                for record in page_items
            ],
        }

    def get_recent_task_report(self, hours: int = 24, limit: int = 200) -> Dict[str, Any]:
        safe_hours = max(1, hours)
        safe_limit = min(max(1, limit), 1000)
        now = datetime.now(timezone.utc)
        since = now - timedelta(hours=safe_hours)
        records = [record for record in self.list_all() if record.created_at >= since]
        records.sort(key=lambda item: item.created_at, reverse=True)
        limited_records = records[:safe_limit]
        usage_by_task = self._usage_by_task_id(limited_records)

        rows = [self._recent_task_row(record, usage_by_task.get(record.task_id)) for record in limited_records]
        gate_counts = Counter(row["gate_status"] for row in rows)
        status_counts = Counter(row["status"] for row in rows)
        app_counts = Counter(row["app_name"] for row in rows)
        severity_counts: Counter[str] = Counter()
        for row in rows:
            severity_counts.update(row["severity_counts"])

        return {
            "generated_at": now.isoformat(),
            "window_start": since.isoformat(),
            "window_end": now.isoformat(),
            "hours": safe_hours,
            "limit": safe_limit,
            "total_matched": len(records),
            "total_returned": len(rows),
            "summary": {
                "total": len(rows),
                "passed": gate_counts.get("passed", 0),
                "failed": gate_counts.get("failed", 0),
                "running_or_queued": gate_counts.get("running", 0) + gate_counts.get("queued", 0),
                "findings": sum(row["findings_count"] for row in rows),
                "callback_succeeded": sum(1 for row in rows if row["callback_succeeded"]),
                "tokens": sum(row["token_usage"]["total_tokens"] for row in rows),
            },
            "status_counts": dict(sorted(status_counts.items())),
            "gate_counts": dict(sorted(gate_counts.items())),
            "app_counts": dict(app_counts.most_common(12)),
            "severity_counts": dict(sorted(severity_counts.items())),
            "tasks": rows,
        }

    def _recent_task_row(self, record: TaskRecord, token_usage: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        result = record.result
        findings = result.findings if result is not None else []
        severity_counts = Counter(finding.severity.value for finding in findings)
        duration_seconds = None
        if record.started_at and record.finished_at:
            duration_seconds = round((record.finished_at - record.started_at).total_seconds(), 1)

        return {
            "task_id": record.task_id,
            "app_name": record.request.app_name,
            "repo_url": record.request.repo_url,
            "branch": record.request.branch,
            "commit_id": record.request.commit_id,
            "operator": record.request.operator,
            "trigger_source": record.request.trigger_source,
            "ci_task_id": record.request.ci_task_id,
            "ci_record_id": record.request.ci_record_id,
            "status": record.status.value,
            "gate_status": self._recent_gate_status(record),
            "pass_check": result.pass_check if result is not None else None,
            "score": result.score if result is not None else None,
            "summary": result.summary if result is not None else None,
            "findings_count": len(findings),
            "severity_counts": dict(sorted(severity_counts.items())),
            "attempts": record.attempts,
            "callback_succeeded": record.callback_succeeded,
            "created_at": record.created_at.isoformat(),
            "updated_at": record.updated_at.isoformat(),
            "started_at": record.started_at.isoformat() if record.started_at else None,
            "finished_at": record.finished_at.isoformat() if record.finished_at else None,
            "duration_seconds": duration_seconds,
            "report_url": record.report_url,
            "error_message": record.error_message,
            "token_usage": token_usage or self._empty_token_usage(),
        }

    def _usage_by_task_id(self, records: List[TaskRecord]) -> Dict[str, Dict[str, Any]]:
        usage_by_task = {record.task_id: self._empty_token_usage() for record in records}
        for record in records:
            usage = usage_by_task.setdefault(record.task_id, self._empty_token_usage())
            db_usage = self._opencode_db_usage_for_record(record)
            if db_usage["total_tokens"] <= 0:
                continue
            usage["source"] = "opencode_db"
            usage["categories"]["analysis"] = {
                "calls": db_usage["calls"],
                "prompt_tokens": db_usage["prompt_tokens"],
                "completion_tokens": db_usage["completion_tokens"],
                "thinking_tokens": db_usage["thinking_tokens"],
                "cache_read_tokens": db_usage.get("cache_read_tokens", 0),
                "total_tokens": db_usage["total_tokens"],
            }
            self._add_usage_stats(usage, usage["categories"]["analysis"])
        for usage in usage_by_task.values():
            usage.update(self._token_costs(usage))
        return usage_by_task

    @staticmethod
    def _empty_token_usage() -> Dict[str, Any]:
        return {
            "calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "thinking_tokens": 0,
            "cache_read_tokens": 0,
            "total_tokens": 0,
            "input_cost_usd": 0.0,
            "output_cost_usd": 0.0,
            "cost_usd": 0.0,
            "source": "usage_log",
            "categories": {},
        }

    @staticmethod
    def _add_usage_stats(usage: Dict[str, Any], stats: Dict[str, int]) -> None:
        usage["calls"] += stats["calls"]
        usage["prompt_tokens"] += stats["prompt_tokens"]
        usage["completion_tokens"] += stats["completion_tokens"]
        usage["thinking_tokens"] += stats["thinking_tokens"]
        usage["cache_read_tokens"] += stats.get("cache_read_tokens", 0)
        usage["total_tokens"] += stats["total_tokens"]

    @staticmethod
    def _token_costs(usage: Dict[str, Any]) -> Dict[str, float]:
        input_tokens = usage["prompt_tokens"] + usage.get("cache_read_tokens", 0)
        input_cost = float(input_tokens) * 1.25 / 1_000_000
        output_cost = float(usage["completion_tokens"] + usage["thinking_tokens"]) * 10.0 / 1_000_000
        return {
            "input_cost_usd": round(input_cost, 4),
            "output_cost_usd": round(output_cost, 4),
            "cost_usd": round(input_cost + output_cost, 4),
        }

    def _opencode_db_usage_for_record(self, record: TaskRecord) -> Dict[str, int]:
        return OpencodeRunner._load_usage_metrics_from_opencode_db(record.opencode_session_ids)

    @staticmethod
    def _recent_gate_status(record: TaskRecord) -> str:
        if record.status in (TaskStatus.queued, TaskStatus.running):
            return record.status.value
        if record.result is None:
            return "failed" if record.status == TaskStatus.failed else record.status.value
        return "passed" if record.result.pass_check else "failed"

    def list_fix_sessions_page(
        self,
        start_at: Optional[datetime] = None,
        end_at: Optional[datetime] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        rows: List[Dict[str, Any]] = []
        for record in self.list_all():
            for session in self.feedback.load_fix_sessions(record.task_id):
                created_at = session.created_at
                if start_at and created_at < start_at:
                    continue
                if end_at and created_at > end_at:
                    continue
                rows.append(
                    {
                        "session_id": session.session_id,
                        "task_id": record.task_id,
                        "app_name": record.request.app_name,
                        "branch": record.request.branch,
                        "created_at": session.created_at.isoformat(),
                        "updated_at": session.updated_at.isoformat(),
                        "stage": session.stage.value,
                        "processing": session.processing,
                        "merge_request_url": session.merge_request_url,
                        "report_url": record.report_url,
                    }
                )
        rows.sort(key=lambda item: item["created_at"], reverse=True)
        total = len(rows)
        safe_page_size = max(page_size, 1)
        total_pages = max(1, (total + safe_page_size - 1) // safe_page_size)
        safe_page = min(max(page, 1), total_pages)
        start_index = (safe_page - 1) * safe_page_size
        end_index = start_index + safe_page_size
        return {
            "total": total,
            "page": safe_page,
            "page_size": safe_page_size,
            "items": rows[start_index:end_index],
        }

    def get_report_detail(self, task_id: str) -> ReportDetail:
        record = self.get(task_id)
        if record is None or record.result is None:
            raise RuntimeError("report not ready")

        effective = self._build_effective_report_state(record)
        return ReportDetail(
            task_id=record.task_id,
            app_name=record.request.app_name,
            branch=record.request.branch,
            commit_id=record.request.commit_id,
            generated_at=record.updated_at,
            summary=effective["summary"],
            score=effective["score"],
            pass_check=effective["pass_check"],
            report_url=record.report_url,
            findings=effective["views"],
            general_feedbacks=self.feedback.load_general_feedbacks(task_id),
            fix_sessions=self._build_fix_session_views(record, effective["views"]),
        )

    def submit_finding_feedback(self, task_id: str, finding_index: int, message: str) -> FindingFeedbackThread:
        record = self.get(task_id)
        if record is None or record.result is None:
            raise RuntimeError("report not ready")
        if finding_index < 0 or finding_index >= len(record.result.findings):
            raise RuntimeError("finding not found")

        threads = self.feedback.load_threads(task_id)
        thread = threads.get(finding_index, FindingFeedbackThread(finding_index=finding_index))
        thread.messages.append(FeedbackMessage(role=FeedbackRole.user, content=message))
        thread.processing = True
        thread.updated_at = datetime.now(timezone.utc)
        threads[finding_index] = thread
        self.feedback.save_threads(task_id, threads)
        self._feedback_queue.put_nowait((task_id, finding_index))
        return thread

    def submit_general_feedback(self, task_id: str, message: str) -> GeneralFeedbackItem:
        record = self.get(task_id)
        if record is None or record.result is None:
            raise RuntimeError("report not ready")
        item = GeneralFeedbackItem(
            feedback_id=uuid.uuid4().hex,
            content=message,
            processing=True,
        )
        items = self.feedback.load_general_feedbacks(task_id)
        items.append(item)
        self.feedback.save_general_feedbacks(task_id, items)
        self._general_feedback_queue.put_nowait((task_id, item.feedback_id))
        return item

    def create_fix_session(
        self,
        task_id: str,
        selected_finding_indexes: List[int],
        message: str,
    ) -> FixSession:
        record = self.get(task_id)
        if record is None or record.result is None:
            raise RuntimeError("report not ready")
        selected = self._normalize_fix_selection(record, selected_finding_indexes)
        if not selected:
            raise RuntimeError("no fixable findings selected")
        sessions = self.feedback.load_fix_sessions(task_id)
        session = FixSession(
            session_id=uuid.uuid4().hex,
            selected_finding_indexes=selected,
            messages=[],
        )
        if message.strip():
            session.messages.append(FeedbackMessage(role=FeedbackRole.user, content=message.strip()))
        session.processing = True
        session.updated_at = datetime.now(timezone.utc)
        sessions.append(session)
        self.feedback.save_fix_sessions(task_id, sessions)
        self._fix_queue.put_nowait((task_id, session.session_id))
        return session

    def submit_fix_session_message(self, task_id: str, session_id: str, message: str) -> FixSession:
        record = self.get(task_id)
        if record is None or record.result is None:
            raise RuntimeError("report not ready")
        sessions = self.feedback.load_fix_sessions(task_id)
        session = next((item for item in sessions if item.session_id == session_id), None)
        if session is None:
            raise RuntimeError("fix session not found")
        if session.stage == FixSessionStage.completed:
            raise RuntimeError("fix session already completed")
        if message.strip():
            session.messages.append(FeedbackMessage(role=FeedbackRole.user, content=message.strip()))
        if session.stage == FixSessionStage.awaiting_user_confirmation and self._is_user_fix_confirmed(message):
            session.stage = FixSessionStage.completed
            session.processing = False
            session.last_result = "用户确认通过，本次修复会话已关闭。"
            session.messages.append(FeedbackMessage(role=FeedbackRole.model, content=session.last_result))
            session.updated_at = datetime.now(timezone.utc)
            self._replace_fix_session(sessions, session)
            self.feedback.save_fix_sessions(task_id, sessions)
            return session
        session.processing = True
        if session.stage in (
            FixSessionStage.awaiting_user_confirmation,
            FixSessionStage.completed,
            FixSessionStage.failed,
        ):
            session.stage = FixSessionStage.scope_confirmation
        session.updated_at = datetime.now(timezone.utc)
        self._replace_fix_session(sessions, session)
        self.feedback.save_fix_sessions(task_id, sessions)
        self._fix_queue.put_nowait((task_id, session.session_id))
        return session

    def _feedback_loop(self) -> None:
        while True:
            task_id, finding_index = self._feedback_queue.get()
            try:
                self._process_feedback_job(task_id, finding_index)
            except Exception:  # noqa: BLE001
                logger.exception("task=%s feedback processing crashed finding_index=%s", task_id, finding_index)
            finally:
                self._feedback_queue.task_done()

    def _general_feedback_loop(self) -> None:
        while True:
            task_id, feedback_id = self._general_feedback_queue.get()
            try:
                self._process_general_feedback_job(task_id, feedback_id)
            except Exception:  # noqa: BLE001
                logger.exception("task=%s general feedback processing crashed feedback_id=%s", task_id, feedback_id)
            finally:
                self._general_feedback_queue.task_done()

    def _fix_loop(self) -> None:
        while True:
            task_id, session_id = self._fix_queue.get()
            try:
                self._process_fix_job(task_id, session_id)
            except Exception:  # noqa: BLE001
                logger.exception("task=%s fix processing crashed session_id=%s", task_id, session_id)
            finally:
                self._fix_queue.task_done()

    def _process_feedback_job(self, task_id: str, finding_index: int) -> None:
        record = self.get(task_id)
        if record is None or record.result is None:
            return
        if finding_index < 0 or finding_index >= len(record.result.findings):
            return

        finding = record.result.findings[finding_index]
        threads = self.feedback.load_threads(task_id)
        thread = threads.get(finding_index)
        if thread is None:
            return

        try:
            with self.git.repo_lock(record.request.repo_url):
                repo_path = self.git.prepare_repo(
                    repo_url=record.request.repo_url,
                    branch=record.request.branch,
                    commit_id=record.request.commit_id,
                    task_id=task_id,
                    is_cancelled=self._is_task_cancelled,
                )
                code_context = self._load_code_context(repo_path, record.request.commit_id, finding)

            review = self.runner.review_finding_feedback(
                task_id=task_id,
                repo_path=repo_path,
                commit_id=record.request.commit_id,
                finding={
                    "index": finding_index,
                    "file": finding.file,
                    "line": finding.line,
                    "severity": finding.severity.value,
                    "title": finding.title,
                    "detail": finding.detail,
                    "suggestion": finding.suggestion,
                },
                code_context=code_context,
                conversation=[item.model_dump(mode="json") for item in thread.messages],
                is_cancelled=self._is_task_cancelled,
            )
            thread.messages.append(FeedbackMessage(role=FeedbackRole.model, content=review["reply"]))
            action = review["action"]
            if action == "downgrade":
                thread.status = FindingStatus.severity_adjusted
                thread.current_severity = FindingSeverity(review["severity"])
                thread.pattern_summary = review.get("pattern_summary") or finding.title
                self._append_accepted_finding_pattern(
                    task_id=task_id,
                    record=record,
                    finding_index=finding_index,
                    finding=finding,
                    thread=thread,
                    action=action,
                )
            elif action == "resolve_false_positive":
                thread.status = FindingStatus.resolved_model_false_positive
                thread.pattern_summary = review.get("pattern_summary") or finding.title
                self._append_accepted_finding_pattern(
                    task_id=task_id,
                    record=record,
                    finding_index=finding_index,
                    finding=finding,
                    thread=thread,
                    action=action,
                )
            else:
                thread.status = FindingStatus.open
        except Exception as exc:  # noqa: BLE001
            thread.messages.append(FeedbackMessage(role=FeedbackRole.model, content=f"模型复核失败：{exc}"))
        finally:
            thread.processing = False
            thread.updated_at = datetime.now(timezone.utc)
            threads[finding_index] = thread
            self.feedback.save_threads(task_id, threads)
            self._refresh_record_after_feedback(task_id)

    def _process_general_feedback_job(self, task_id: str, feedback_id: str) -> None:
        record = self.get(task_id)
        if record is None or record.result is None:
            return
        items = self.feedback.load_general_feedbacks(task_id)
        item = next((entry for entry in items if entry.feedback_id == feedback_id), None)
        if item is None:
            return
        try:
            with self.git.repo_lock(record.request.repo_url):
                repo_path = self.git.prepare_repo(
                    repo_url=record.request.repo_url,
                    branch=record.request.branch,
                    commit_id=record.request.commit_id,
                    task_id=task_id,
                    is_cancelled=self._is_task_cancelled,
                )
            review = self.runner.review_missed_issue_feedback(
                task_id=task_id,
                repo_path=repo_path,
                branch=record.request.branch,
                commit_id=record.request.commit_id,
                existing_summary=record.result.summary,
                missed_issue=item.content,
                is_cancelled=self._is_task_cancelled,
            )
            item.confirmed = review["confirmed"]
            item.reply = review["reply"]
            item.pattern_summary = review.get("pattern_summary")
            if item.confirmed:
                self.feedback.append_missed_issue_pattern(
                    task_id=task_id,
                    app_name=record.request.app_name,
                    branch=record.request.branch,
                    commit_id=record.request.commit_id,
                    content=item.content,
                    reply=item.reply,
                    pattern_summary=item.pattern_summary,
                )
        except Exception as exc:  # noqa: BLE001
            item.confirmed = False
            item.reply = f"模型复核失败：{exc}"
        finally:
            item.processing = False
            item.updated_at = datetime.now(timezone.utc)
            for idx, current in enumerate(items):
                if current.feedback_id == feedback_id:
                    items[idx] = item
                    break
            self.feedback.save_general_feedbacks(task_id, items)

    def _process_fix_job(self, task_id: str, session_id: str) -> None:
        record = self.get(task_id)
        if record is None or record.result is None:
            return
        sessions = self.feedback.load_fix_sessions(task_id)
        session = next((item for item in sessions if item.session_id == session_id), None)
        if session is None:
            return
        try:
            if session.stage in (FixSessionStage.scope_confirmation, FixSessionStage.plan_confirmation):
                selected_findings = self._selected_finding_payload(record, session.selected_finding_indexes)
                guidance = self.runner.review_fix_conversation(
                    task_id=task_id,
                    repo_url=record.request.repo_url,
                    app_name=record.request.app_name,
                    branch=record.request.branch,
                    commit_id=record.request.commit_id,
                    stage=session.stage.value,
                    selected_findings=selected_findings,
                    scope_summary=session.scope_summary,
                    plan_summary=session.plan_summary,
                    conversation=[item.model_dump(mode="json") for item in session.messages],
                )
                session.messages.append(FeedbackMessage(role=FeedbackRole.model, content=guidance["reply"]))
                session.target_repo_url = guidance.get("target_repo_url") or session.target_repo_url or record.request.repo_url
                session.target_branch = guidance.get("target_branch") or session.target_branch or record.request.branch
                session.scope_summary = guidance.get("scope_summary") or session.scope_summary
                session.plan_summary = guidance.get("plan_summary") or session.plan_summary
                next_stage = FixSessionStage(guidance["next_stage"])
                session.stage = next_stage
                if next_stage == FixSessionStage.fixing:
                    self._replace_fix_session(sessions, session)
                    self.feedback.save_fix_sessions(task_id, sessions)
                    self._execute_fix_session(record, session, sessions)
                    return
            elif session.stage == FixSessionStage.fixing:
                self._execute_fix_session(record, session, sessions)
                return
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "task=%s fix session failed session_id=%s stage=%s round=%s",
                task_id,
                session_id,
                session.stage,
                session.execution_round,
            )
            session.stage = FixSessionStage.failed
            session.last_result = f"修复处理失败：{exc}"
            session.messages.append(FeedbackMessage(role=FeedbackRole.model, content=session.last_result))
        finally:
            session.processing = False
            session.updated_at = datetime.now(timezone.utc)
            self._replace_fix_session(sessions, session)
            self.feedback.save_fix_sessions(task_id, sessions)

    def _execute_fix_session(self, record: TaskRecord, session: FixSession, sessions: List[FixSession]) -> None:
        selected_findings = self._selected_finding_payload(record, session.selected_finding_indexes)
        if not selected_findings:
            raise RuntimeError("no fixable findings remain")

        session.processing = True
        session.stage = FixSessionStage.fixing
        session.execution_round += 1
        workspace_dir, repo_path, fix_branch = self._prepare_fix_workspace(record, session)
        session.workspace_dir = str(workspace_dir)
        session.source_branch = fix_branch
        self._notify_fix_progress(record, session, phase="started")
        self._replace_fix_session(sessions, session)
        self.feedback.save_fix_sessions(record.task_id, sessions)

        summary = ""
        for attempt in range(1, self.settings.fix_loop_max_rounds + 1):
            summary = self.runner.apply_fix_session(
                task_id=record.task_id,
                repo_path=repo_path,
                repo_url=record.request.repo_url,
                branch=record.request.branch,
                commit_id=record.request.commit_id,
                selected_findings=selected_findings,
                scope_summary=session.scope_summary,
                plan_summary=session.plan_summary,
                conversation=[item.model_dump(mode="json") for item in session.messages],
                attempt=attempt,
                fix_skill_path=(self.settings.fix_skill_dir / "SKILL.md").resolve(),
            )
            review = self.runner.review_fix_result(
                task_id=record.task_id,
                repo_path=repo_path,
                repo_url=record.request.repo_url,
                branch=record.request.branch,
                commit_id=record.request.commit_id,
                selected_findings=selected_findings,
                summary=summary,
                scope_summary=session.scope_summary,
                plan_summary=session.plan_summary,
            )
            if review["passed"]:
                commit_message = f"fix: address selected findings for {record.request.app_name}"
                if not self.git.commit_all_and_push(repo_path, fix_branch, commit_message):
                    raise RuntimeError("fix flow produced no code changes")
                mr_url = self._create_fix_merge_request(record, fix_branch, session)
                session.merge_request_url = mr_url
                session.last_result = review["reply"]
                session.stage = FixSessionStage.awaiting_user_confirmation
                session.messages.append(
                    FeedbackMessage(
                        role=FeedbackRole.model,
                        content=f"{review['reply']}\n\n修复已提交，MR: {mr_url}",
                    )
                )
                self._notify_fix_progress(record, session, phase="completed", mr_url=mr_url)
                return
            summary = f"{summary}\n\n复核未通过：{review['reply']}"

        raise RuntimeError("fix review did not pass within retry limit")

    def _prepare_fix_workspace(self, record: TaskRecord, session: FixSession) -> tuple[Path, Path, str]:
        app = self._slugify(record.request.app_name)
        target_repo_url = session.target_repo_url or record.request.repo_url
        target_repo_name = self._slugify(Path(self._repo_project_path(target_repo_url)).name)
        branch = self._slugify(session.target_branch or record.request.branch)
        commit = (record.request.commit_id or "head")[:12]
        workspace_name = f"{app}-{target_repo_name}-{branch}-{commit}-{session.session_id[:8]}-r{session.execution_round}"
        workspace_dir = self.settings.fix_space_dir / workspace_name
        fix_branch = f"codex/fix-{app}-{target_repo_name}-{session.session_id[:8]}-r{session.execution_round}"
        repo_path = self.git.prepare_fix_workspace(
            repo_url=session.target_repo_url or record.request.repo_url,
            branch=session.target_branch or record.request.branch,
            commit_id=record.request.commit_id,
            workspace_dir=workspace_dir,
            fix_branch=fix_branch,
            task_id=record.task_id,
            is_cancelled=self._is_task_cancelled,
        )
        return workspace_dir, repo_path, fix_branch

    def _create_fix_merge_request(self, record: TaskRecord, source_branch: str, session: FixSession) -> str:
        target_repo_url = session.target_repo_url or record.request.repo_url
        target_branch = session.target_branch or record.request.branch
        project_path = self._repo_project_path(target_repo_url)
        project = urllib.parse.quote_plus(project_path)
        url = f"{self.settings.gitlab_base_url}/api/v4/projects/{project}/merge_requests"
        payload = urllib.parse.urlencode(
            {
                "source_branch": source_branch,
                "target_branch": target_branch,
                "title": f"fix: {record.request.app_name} findings {session.session_id[:8]}",
                "description": f"Auto-generated by cr_agent task {record.task_id}.",
            }
        ).encode("utf-8")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request = urllib.request.Request(
            url,
            data=payload,
            headers={
                "PRIVATE-TOKEN": self.settings.gitlab_api_token,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        with opener.open(request, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8"))
        return data.get("web_url") or f"{self.settings.gitlab_base_url}/{project_path}/-/merge_requests"

    def _notify_fix_progress(
        self,
        record: TaskRecord,
        session: FixSession,
        *,
        phase: str,
        mr_url: Optional[str] = None,
    ) -> None:
        if not self.settings.ci_notice_url:
            return
        ticket_id = (record.request.branch or "").strip()
        if not re.match(r"^[A-Za-z]+-\d+$", ticket_id):
            logger.info("task=%s skip fix notify invalid ticket branch=%s", record.task_id, ticket_id)
            return

        selected_titles = []
        if record.result is not None:
            for index in session.selected_finding_indexes:
                if 0 <= index < len(record.result.findings):
                    selected_titles.append(record.result.findings[index].title)
        title_text = "；".join(selected_titles) if selected_titles else "未指定问题"
        if phase == "started":
            msg = (
                f"【大模型修复启动】\n"
                f"应用: {record.request.app_name}\n"
                f"分支/Ticket: {ticket_id}\n"
                f"会话: {session.session_id}\n"
                f"轮次: {session.execution_round}\n"
                f"问题: {title_text}\n"
                f"报告: {record.report_url or '-'}"
            )
        else:
            msg = (
                f"【大模型修复完成】\n"
                f"应用: {record.request.app_name}\n"
                f"分支/Ticket: {ticket_id}\n"
                f"会话: {session.session_id}\n"
                f"轮次: {session.execution_round}\n"
                f"问题: {title_text}\n"
                f"MR: {mr_url or session.merge_request_url or '-'}\n"
                f"报告: {record.report_url or '-'}"
            )
        query = urllib.parse.urlencode({"msg": msg, "ticketId": ticket_id, "atAll": "true"})
        url = f"{self.settings.ci_notice_url}?{query}"
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(url, timeout=10) as response:
                body = response.read().decode("utf-8", errors="replace")
            logger.info(
                "task=%s fix notify sent phase=%s ticket=%s response=%s",
                record.task_id,
                phase,
                ticket_id,
                body[:500],
            )
        except Exception:  # noqa: BLE001
            logger.exception("task=%s fix notify failed phase=%s ticket=%s", record.task_id, phase, ticket_id)

    def _append_accepted_finding_pattern(
        self,
        *,
        task_id: str,
        record: TaskRecord,
        finding_index: int,
        finding: Finding,
        thread: FindingFeedbackThread,
        action: str,
    ) -> None:
        try:
            self.feedback.append_issue_pattern(
                task_id=task_id,
                app_name=record.request.app_name,
                branch=record.request.branch,
                commit_id=record.request.commit_id,
                finding_index=finding_index,
                file=finding.file,
                title=finding.title,
                action=action,
                severity=(thread.current_severity.value if thread.current_severity else finding.severity.value),
                conversation=[item.model_dump(mode="json") for item in thread.messages],
                pattern_summary=thread.pattern_summary,
            )
            logger.info(
                "task=%s accepted finding feedback archived finding_index=%s action=%s",
                task_id,
                finding_index,
                action,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "task=%s accepted finding feedback archive failed finding_index=%s action=%s",
                task_id,
                finding_index,
                action,
            )

    def _worker_loop(self) -> None:
        while True:
            task_id = self._queue.get()
            self._mark_task_active(task_id)
            logger.info("task=%s worker picked up task", task_id)
            try:
                self._run_task(task_id)
            except Exception:  # noqa: BLE001
                logger.exception("task execution crashed: %s", task_id)
            finally:
                self._mark_task_inactive(task_id)
                logger.info("task=%s worker finished task loop", task_id)
                self._queue.task_done()

    def _run_task(self, task_id: str) -> None:
        record = self.store.get(task_id)
        if record is None:
            return

        if self._is_task_deleted(task_id):
            logger.info("task=%s run skipped because task was deleted", task_id)
            return

        now = datetime.now(timezone.utc)
        record.status = TaskStatus.running
        record.started_at = now
        record.attempts += 1
        self._save_record(record)
        logger.info(
            "task=%s run start attempt=%s app=%s branch=%s repo=%s",
            task_id,
            record.attempts,
            record.request.app_name,
            record.request.branch,
            record.request.repo_url,
        )

        try:
            with self.git.repo_lock(record.request.repo_url):
                repo_path = self.git.prepare_repo(
                    repo_url=record.request.repo_url,
                    branch=record.request.branch,
                    commit_id=record.request.commit_id,
                    task_id=task_id,
                    is_cancelled=self._is_task_cancelled,
                )
                resolved_commit_id = self.git.current_commit(
                    repo_path,
                    task_id=task_id,
                    is_cancelled=self._is_task_cancelled,
                )
                if not record.request.commit_id:
                    record.request.commit_id = resolved_commit_id
                    self._save_record(record)
                review_context = self.git.collect_review_context(
                    repo_path,
                    task_id=task_id,
                    is_cancelled=self._is_task_cancelled,
                )
            logger.info(
                "task=%s repo ready path=%s commit=%s diff_range=%s non_test_changed_files=%s",
                task_id,
                repo_path,
                record.request.commit_id or resolved_commit_id,
                review_context.get("diff_range"),
                len(review_context.get("non_test_changed_files") or []),
            )
            metadata = dict(record.request.metadata)
            metadata["review_context"] = review_context
            result = self.runner.analyze(
                task_id=record.task_id,
                repo_path=repo_path,
                branch=record.request.branch,
                commit_id=record.request.commit_id,
                metadata=metadata,
                is_cancelled=self._is_task_cancelled,
            )
            if self._is_task_deleted(task_id):
                logger.info("task=%s result discarded because task was deleted", task_id)
                return
            record.opencode_session_ids = self._result_opencode_session_ids(result)
            result = self._normalize_result(result, review_context)
            logger.info(
                "task=%s analysis normalized findings=%s score=%s pass_check=%s",
                task_id,
                len(result.findings),
                result.score,
                result.pass_check,
            )
            report_url, report_file = self.reporter.write(record, result)
            logger.info(
                "task=%s report written report_url=%s report_file=%s",
                task_id,
                report_url,
                report_file,
            )

            record.status = TaskStatus.success if result.pass_check else TaskStatus.failed
            record.finished_at = datetime.now(timezone.utc)
            record.result = result
            record.report_url = report_url
            record.report_file = report_file
            self._save_record(record)
            logger.info(
                "task=%s run reached terminal status=%s score=%s findings=%s",
                task_id,
                record.status,
                result.score,
                len(result.findings),
            )

            payload = CallbackPayload(
                task_id=record.task_id,
                app_name=record.request.app_name,
                branch=record.request.branch,
                commit_id=record.request.commit_id,
                passed=result.pass_check,
                score=result.score,
                report_url=report_url,
                status=record.status,
                summary=result.summary,
                findings_count=len(result.findings),
                metadata=record.request.metadata,
            )
            self._send_callback_if_needed(record, payload)
        except Exception as exc:  # noqa: BLE001
            if self._is_task_deleted(task_id):
                logger.info("task=%s deleted during execution; suppressing failure persistence", task_id)
                return
            if self._should_retry_task(record, exc):
                self._requeue_after_failure(record, reason=str(exc))
                return
            record.status = TaskStatus.failed
            record.error_message = str(exc)
            record.finished_at = datetime.now(timezone.utc)
            self._save_record(record)
            logger.exception("task=%s run failed error=%s", task_id, exc)
            raise

    @staticmethod
    def _result_opencode_session_ids(result: AnalysisResult) -> List[str]:
        raw_output = result.raw_output or {}
        values = raw_output.get("_opencode_session_ids") if isinstance(raw_output, dict) else None
        if not isinstance(values, list):
            return []
        session_ids: List[str] = []
        for value in values:
            if isinstance(value, str) and value and value not in session_ids:
                session_ids.append(value)
        return session_ids

    def _enqueue_task(self, task_id: str) -> None:
        try:
            self._queue.put_nowait(task_id)
            logger.info("task=%s enqueued queue_size=%s", task_id, self._queue.qsize())
        except queue.Full as exc:
            raise RuntimeError("task queue is full") from exc

    def _find_inflight_duplicate(self, request: TriggerRequest) -> Optional[TaskRecord]:
        request_key = self._request_key(request)
        for record in reversed(self.list_all()):
            if record.status not in (TaskStatus.queued, TaskStatus.running):
                continue
            if self._request_key(record.request) == request_key:
                return record
        return None

    def _find_same_commit_record(self, request: TriggerRequest) -> Optional[TaskRecord]:
        if not request.commit_id:
            return None
        request_key = self._request_key(request)
        for record in reversed(self.list_all()):
            if self._request_key(record.request) == request_key:
                return record
        return None

    @staticmethod
    def _request_key(request: TriggerRequest) -> tuple[str, str, str, Optional[str]]:
        return (
            request.app_name,
            request.repo_url,
            request.branch,
            request.commit_id,
        )

    def _recover_pending_tasks(self) -> None:
        logger.info("recovery start pending task scan")
        for record in self.list_all():
            if self._is_task_deleted(record.task_id):
                continue
            if record.status == TaskStatus.queued:
                logger.info("task=%s recovery requeue queued task", record.task_id)
                self._enqueue_task(record.task_id)
                continue
            if record.status == TaskStatus.running:
                self._recover_record(record, reason="service restart recovery")
        logger.info("recovery finished pending task scan")

    def _recovery_loop(self) -> None:
        while True:
            time.sleep(self.settings.recovery_scan_interval_seconds)
            self._sweep_orphan_tasks()

    def _sweep_orphan_tasks(self) -> None:
        now = datetime.now(timezone.utc)
        for record in self.list_all():
            if record.status != TaskStatus.running:
                continue
            if self._is_task_active(record.task_id):
                continue
            if record.started_at is None:
                self._recover_record(record, reason="orphan running task without started_at")
                continue
            elapsed = (now - record.started_at).total_seconds()
            if elapsed >= self.settings.orphan_task_timeout_seconds:
                self._recover_record(record, reason="orphan running task timeout")

    def _recover_record(self, record: TaskRecord, reason: str) -> None:
        if record.attempts >= self.settings.max_task_attempts:
            record.status = TaskStatus.failed
            record.finished_at = datetime.now(timezone.utc)
            record.error_message = f"{reason}; exceeded max attempts={self.settings.max_task_attempts}"
            self._save_record(record)
            logger.warning(
                "task=%s recovery gave up reason=%s attempts=%s",
                record.task_id,
                reason,
                record.attempts,
            )
            return

        record.status = TaskStatus.queued
        record.started_at = None
        record.finished_at = None
        record.error_message = reason
        self._save_record(record)
        logger.warning(
            "task=%s recovery requeued reason=%s next_attempt=%s",
            record.task_id,
            reason,
            record.attempts + 1,
        )
        self._enqueue_task(record.task_id)

    def _requeue_after_failure(self, record: TaskRecord, reason: str) -> None:
        if record.attempts >= self.settings.max_task_attempts:
            record.status = TaskStatus.failed
            record.finished_at = datetime.now(timezone.utc)
            record.error_message = f"{reason}; exceeded max attempts={self.settings.max_task_attempts}"
            self._save_record(record)
            logger.warning(
                "task=%s retry gave up reason=%s attempts=%s",
                record.task_id,
                reason,
                record.attempts,
            )
            return

        record.status = TaskStatus.queued
        record.started_at = None
        record.finished_at = None
        record.error_message = reason
        self._save_record(record)
        logger.warning(
            "task=%s retry requeued reason=%s next_attempt=%s",
            record.task_id,
            reason,
            record.attempts + 1,
        )
        self._enqueue_task(record.task_id)

    def _should_retry_task(self, record: TaskRecord, exc: Exception) -> bool:
        return record.attempts < self.settings.max_task_attempts and "opencode prompt timeout" in str(exc)

    def _mark_task_active(self, task_id: str) -> None:
        with self._active_lock:
            self._active_tasks.add(task_id)

    def _mark_task_inactive(self, task_id: str) -> None:
        with self._active_lock:
            self._active_tasks.discard(task_id)

    def _is_task_active(self, task_id: str) -> bool:
        with self._active_lock:
            return task_id in self._active_tasks

    def _normalize_result(self, result: AnalysisResult, review_context: dict) -> AnalysisResult:
        allowed_files_raw = review_context.get("non_test_changed_files")
        allowed_files = set(allowed_files_raw or [])
        findings = [
            item
            for item in result.findings
            if not self._is_test_file(item.file)
            and (allowed_files_raw is None or item.file in allowed_files)
        ]
        severity_counts = Counter(item.severity for item in findings)
        return AnalysisResult(
            summary=self._build_summary(findings, len(allowed_files)),
            pass_check=severity_counts[FindingSeverity.fatal] == 0 and severity_counts[FindingSeverity.high] == 0,
            score=self._calculate_score(findings),
            findings=findings,
            raw_output=result.raw_output,
        )

    @staticmethod
    def _is_test_file(path: str) -> bool:
        lowered = path.lower()
        return (
            "/src/test/" in lowered
            or "/src/main/test/" in lowered
            or lowered.endswith("test.java")
            or lowered.endswith("tests.java")
            or lowered.endswith("it.java")
        )

    @staticmethod
    def _calculate_score(findings: List[Finding]) -> int:
        penalties = {
            FindingSeverity.fatal: 30,
            FindingSeverity.high: 15,
            FindingSeverity.medium: 8,
            FindingSeverity.low: 3,
            FindingSeverity.info: 0,
        }
        score = 100 - sum(penalties[item.severity] for item in findings)
        return max(score, 0)

    @staticmethod
    def _build_summary(findings: List[Finding], changed_file_count: int) -> str:
        if not findings:
            return f"本次变更共审查 {changed_file_count} 个非测试文件，未发现阻断发布的问题。"

        counts = Counter(item.severity for item in findings)
        parts = []
        for severity in (
            FindingSeverity.fatal,
            FindingSeverity.high,
            FindingSeverity.medium,
            FindingSeverity.low,
            FindingSeverity.info,
        ):
            count = counts[severity]
            if count:
                parts.append(f"{severity.value} {count} 个")
        return (
            f"本次变更共审查 {changed_file_count} 个非测试文件，发现 {len(findings)} 个问题，"
            f"其中 {'，'.join(parts)}。"
        )

    @staticmethod
    def _payload_digest(payload: CallbackPayload) -> str:
        body = payload.model_dump(mode="json")
        body.pop("generated_at", None)
        return json.dumps(body, ensure_ascii=False, sort_keys=True)

    def _send_callback_if_needed(self, record: TaskRecord, payload: CallbackPayload) -> None:
        payload_digest = self._payload_digest(payload)
        if record.callback_succeeded and record.callback_payload_digest == payload_digest:
            logger.info("task=%s callback skipped same payload digest", record.task_id)
            return
        logger.info("task=%s callback send start", record.task_id)
        record.callback_history = self.callback.send(record, payload)
        record.callback_succeeded = True
        record.callback_payload_digest = payload_digest
        self._save_record(record)
        logger.info("task=%s callback send finished attempts=%s", record.task_id, len(record.callback_history))

    def _build_effective_report_state(self, record: TaskRecord) -> Dict[str, Any]:
        assert record.result is not None
        threads = self.feedback.load_threads(record.task_id)
        views: List[FindingView] = []
        effective_findings: List[Finding] = []
        for index, finding in enumerate(record.result.findings):
            thread = threads.get(index, FindingFeedbackThread(finding_index=index))
            effective_severity = thread.current_severity or finding.severity
            status = thread.status
            status_label = {
                FindingStatus.open: "待处理",
                FindingStatus.severity_adjusted: "级别已调整",
                FindingStatus.resolved_model_false_positive: "已解决-模型误判",
            }[status]
            if status != FindingStatus.resolved_model_false_positive:
                effective_findings.append(
                    Finding(
                        file=finding.file,
                        line=finding.line,
                        severity=effective_severity,
                        title=finding.title,
                        detail=finding.detail,
                        suggestion=finding.suggestion,
                    )
                )
            views.append(
                FindingView(
                    index=index,
                    file=finding.file,
                    line=finding.line,
                    title=finding.title,
                    detail=finding.detail,
                    suggestion=finding.suggestion,
                    original_severity=finding.severity,
                    effective_severity=effective_severity,
                    status=status,
                    status_label=status_label,
                    processing=thread.processing,
                    gitlab_url=self.reporter.gitlab_blob_url(
                        record.request.repo_url,
                        record.request.commit_id,
                        finding.file,
                        finding.line,
                    ),
                    thread=thread,
                )
            )
        changed_file_count = self._summary_changed_file_count(record.result.summary, len(record.result.findings))
        summary = self._build_summary(effective_findings, changed_file_count)
        score = self._calculate_score(effective_findings)
        severity_counts = Counter(item.severity for item in effective_findings)
        pass_check = severity_counts[FindingSeverity.fatal] == 0 and severity_counts[FindingSeverity.high] == 0
        return {
            "views": views,
            "effective_findings": effective_findings,
            "summary": summary,
            "score": score,
            "pass_check": pass_check,
        }

    @staticmethod
    def _summary_changed_file_count(summary: str, fallback: int) -> int:
        match = re.search(r"共审查\s*(\d+)\s*个非测试文件", summary)
        if match:
            return int(match.group(1))
        return fallback

    def _refresh_record_after_feedback(self, task_id: str) -> None:
        record = self.get(task_id)
        if record is None or record.result is None or record.report_file is None or record.report_url is None:
            return
        previous_pass_check = bool(record.result.pass_check)
        effective = self._build_effective_report_state(record)
        record.result.summary = effective["summary"]
        record.result.score = effective["score"]
        record.result.pass_check = effective["pass_check"]
        record.status = TaskStatus.success if effective["pass_check"] else TaskStatus.failed
        report_url, report_file = self.reporter.write(record, record.result)
        record.report_url = report_url
        record.report_file = report_file
        self._save_record(record)
        payload = CallbackPayload(
            task_id=record.task_id,
            app_name=record.request.app_name,
            branch=record.request.branch,
            commit_id=record.request.commit_id,
            passed=effective["pass_check"],
            score=effective["score"],
            report_url=record.report_url,
            status=record.status,
            summary=effective["summary"],
            findings_count=len(effective["effective_findings"]),
            metadata=record.request.metadata,
        )
        if (not previous_pass_check) and effective["pass_check"]:
            self._send_callback_if_needed(record, payload)

    def _build_fix_session_views(self, record: TaskRecord, finding_views: List[FindingView]) -> List[FixSessionView]:
        title_map = {item.index: item.title for item in finding_views}
        views: List[FixSessionView] = []
        for session in self.feedback.load_fix_sessions(record.task_id):
            views.append(
                FixSessionView(
                    session_id=session.session_id,
                    stage=session.stage,
                    stage_label=self._fix_stage_label(session.stage),
                    selected_finding_indexes=session.selected_finding_indexes,
                    selected_finding_titles=[title_map.get(index, f"问题 {index}") for index in session.selected_finding_indexes],
                    target_repo_url=session.target_repo_url,
                    target_branch=session.target_branch,
                    scope_summary=session.scope_summary,
                    plan_summary=session.plan_summary,
                    processing=session.processing,
                    workspace_dir=session.workspace_dir,
                    source_branch=session.source_branch,
                    merge_request_url=session.merge_request_url,
                    last_result=session.last_result,
                    execution_round=session.execution_round,
                    messages=session.messages,
                    created_at=session.created_at,
                    updated_at=session.updated_at,
                )
            )
        views.sort(key=lambda item: item.created_at, reverse=True)
        return views

    def _normalize_fix_selection(self, record: TaskRecord, selected_indexes: List[int]) -> List[int]:
        effective = self._build_effective_report_state(record)
        allowed = {item.index for item in effective["views"] if item.status != FindingStatus.resolved_model_false_positive}
        return [index for index in selected_indexes if index in allowed]

    def _selected_finding_payload(self, record: TaskRecord, selected_indexes: List[int]) -> List[Dict[str, Any]]:
        findings: List[Dict[str, Any]] = []
        effective = self._build_effective_report_state(record)
        view_map = {item.index: item for item in effective["views"]}
        for index in selected_indexes:
            item = view_map.get(index)
            if item is None or item.status == FindingStatus.resolved_model_false_positive:
                continue
            findings.append(
                {
                    "index": index,
                    "file": item.file,
                    "line": item.line,
                    "title": item.title,
                    "detail": item.detail,
                    "suggestion": item.suggestion,
                    "severity": (item.effective_severity or item.original_severity).value,
                }
            )
        return findings

    @staticmethod
    def _replace_fix_session(sessions: List[FixSession], session: FixSession) -> None:
        for idx, current in enumerate(sessions):
            if current.session_id == session.session_id:
                sessions[idx] = session
                return
        sessions.append(session)

    @staticmethod
    def _is_user_fix_confirmed(message: str) -> bool:
        text = message.strip()
        return any(keyword in text for keyword in ("确认通过", "满意", "通过", "可以关闭", "没问题了"))

    @staticmethod
    def _fix_stage_label(stage: FixSessionStage) -> str:
        return {
            FixSessionStage.scope_confirmation: "确认问题范围和边界",
            FixSessionStage.plan_confirmation: "确认修复方案",
            FixSessionStage.fixing: "自动修复中",
            FixSessionStage.awaiting_user_confirmation: "等待用户确认",
            FixSessionStage.completed: "已完成",
            FixSessionStage.failed: "执行失败",
        }[stage]

    @staticmethod
    def _slugify(value: str) -> str:
        cleaned = "".join(ch if ch.isalnum() else "-" for ch in value.lower())
        cleaned = "-".join(filter(None, cleaned.split("-")))
        return cleaned[:48] or "task"

    @staticmethod
    def _repo_project_path(repo_url: str) -> str:
        if ":" in repo_url:
            project = repo_url.split(":", 1)[1]
        else:
            project = repo_url.rsplit("/", 2)[-2] + "/" + repo_url.rsplit("/", 1)[-1]
        return project.removesuffix(".git").strip("/")

    def _save_record(self, record: TaskRecord) -> None:
        if self._is_task_deleted(record.task_id):
            return
        self.store.save(record)

    def _cancel_task(self, task_id: str) -> None:
        with self._active_lock:
            self._cancelled_tasks.add(task_id)
            self._deleted_tasks.add(task_id)
        self.store.delete(task_id)
        logger.warning("task=%s deleted by admin", task_id)

    def _is_task_cancelled(self, task_id: str) -> bool:
        with self._active_lock:
            return task_id in self._cancelled_tasks

    def _is_task_deleted(self, task_id: str) -> bool:
        with self._active_lock:
            return task_id in self._deleted_tasks

    @staticmethod
    def _bucket_iso(bucket: int) -> str:
        return datetime.fromtimestamp(bucket, tz=timezone.utc).isoformat()

    @staticmethod
    def _percentile(values: List[float], percentile: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * percentile)))
        return round(ordered[index], 2)

    @staticmethod
    def _load_code_context(repo_path, commit_id: Optional[str], finding: Finding) -> str:
        if not commit_id:
            return "N/A"
        target = f"{commit_id}:{finding.file}"
        import subprocess

        completed = subprocess.run(
            ["git", "-C", str(repo_path), "show", target],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if completed.returncode != 0:
            return f"无法读取代码上下文: {completed.stderr}"
        content = completed.stdout.splitlines()
        if finding.line:
            start = max(finding.line - 8, 1)
            end = min(finding.line + 8, len(content))
        else:
            start = 1
            end = min(30, len(content))
        lines = [f"{idx}:{content[idx - 1]}" for idx in range(start, end + 1)]
        return "\n".join(lines)

"""SQLite storage adapted from comain/unit-test-agent `reference/tasks/db.py`.

The reusable pieces are the connection pragmas, WAL mode, busy timeout, short
`BEGIN IMMEDIATE` transactions, idempotent schema creation, event tables,
runner heartbeats, and queue claim shape. Table names and fields are CR-specific.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Dict, Iterator, List, Optional, Union
import uuid

from cr_agent.review_v2.models import json_dumps, normalize_severity, now_iso


TOKEN_COLUMNS = (
    "input_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "output_tokens",
    "reasoning_tokens",
    "total_tokens",
)


class ReviewDB:
    def __init__(self, path: Union[Path, str]):
        self.path = Path(path).expanduser().resolve()

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cr_tasks (
                    task_id TEXT PRIMARY KEY,
                    app_name TEXT NOT NULL,
                    repo_url TEXT NOT NULL,
                    branch TEXT NOT NULL,
                    commit_id TEXT,
                    status TEXT NOT NULL DEFAULT 'queued'
                        CHECK(status IN ('queued', 'running', 'success', 'failed', 'incomplete', 'skipped', 'cancelled')),
                    gate_status TEXT,
                    priority INTEGER NOT NULL DEFAULT 100,
                    queued_at TEXT,
                    not_before_at TEXT,
                    claimed_by TEXT,
                    lease_expires_at TEXT,
                    last_heartbeat_at TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    workflow_run_id TEXT,
                    callback_state TEXT,
                    callback_attempts INTEGER NOT NULL DEFAULT 0,
                    callback_next_retry_at TEXT,
                    callback_last_error TEXT,
                    callback_payload_digest TEXT,
                    callback_history_json TEXT NOT NULL DEFAULT '[]',
                    report_url TEXT,
                    progress_url TEXT,
                    request_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    error TEXT
                );

                CREATE TABLE IF NOT EXISTS reviewer_plans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    workflow_run_id TEXT NOT NULL,
                    reviewer TEXT NOT NULL,
                    required INTEGER NOT NULL DEFAULT 1,
                    risk_tier TEXT,
                    reason TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES cr_tasks(task_id) ON DELETE CASCADE,
                    UNIQUE(task_id, workflow_run_id, reviewer)
                );

                CREATE TABLE IF NOT EXISTS reviewer_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    reviewer TEXT NOT NULL,
                    workflow_run_id TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    status TEXT NOT NULL
                        CHECK(status IN ('queued', 'running', 'success', 'failed', 'cancelled')),
                    session_id TEXT,
                    model_id TEXT,
                    raw_log_path TEXT,
                    output_path TEXT,
                    error TEXT,
                    duration_seconds REAL,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    cost_usd REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    FOREIGN KEY(task_id) REFERENCES cr_tasks(task_id) ON DELETE CASCADE,
                    UNIQUE(task_id, reviewer, workflow_run_id, attempt)
                );

                CREATE TABLE IF NOT EXISTS findings (
                    finding_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    reviewer_run_id INTEGER,
                    file_path TEXT NOT NULL,
                    line INTEGER,
                    severity TEXT NOT NULL CHECK(severity IN ('fatal', 'high', 'medium', 'low', 'info')),
                    title TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    suggestion TEXT,
                    status TEXT NOT NULL DEFAULT 'open',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES cr_tasks(task_id) ON DELETE CASCADE,
                    FOREIGN KEY(reviewer_run_id) REFERENCES reviewer_runs(id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS finding_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    finding_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    actor TEXT,
                    message TEXT,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(finding_id) REFERENCES findings(finding_id) ON DELETE CASCADE,
                    FOREIGN KEY(task_id) REFERENCES cr_tasks(task_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS feedback_sessions (
                    feedback_session_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    finding_id TEXT,
                    parent_reviewer_run_id INTEGER,
                    opencode_session_id TEXT,
                    feedback_text TEXT,
                    model_reply TEXT,
                    status TEXT NOT NULL DEFAULT 'queued',
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    cost_usd REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES cr_tasks(task_id) ON DELETE CASCADE,
                    FOREIGN KEY(finding_id) REFERENCES findings(finding_id) ON DELETE SET NULL,
                    FOREIGN KEY(parent_reviewer_run_id) REFERENCES reviewer_runs(id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS task_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT,
                    event_type TEXT NOT NULL,
                    severity TEXT NOT NULL DEFAULT 'info',
                    stage TEXT,
                    message TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES cr_tasks(task_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS task_controls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    action TEXT NOT NULL CHECK(action IN ('stop', 'cancel')),
                    reason TEXT,
                    requested_at TEXT NOT NULL,
                    acknowledged_at TEXT,
                    FOREIGN KEY(task_id) REFERENCES cr_tasks(task_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS runner_heartbeats (
                    runner_id TEXT PRIMARY KEY,
                    task_id TEXT,
                    pid INTEGER,
                    hostname TEXT,
                    status TEXT NOT NULL,
                    message TEXT,
                    started_at TEXT,
                    heartbeat_at TEXT,
                    loaded_config_hash TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES cr_tasks(task_id) ON DELETE SET NULL
                );
                """
            )
            self._ensure_column(conn, "cr_tasks", "callback_payload_digest", "TEXT")
            self._ensure_column(conn, "cr_tasks", "callback_history_json", "TEXT NOT NULL DEFAULT '[]'")
            self._ensure_column(conn, "cr_tasks", "request_json", "TEXT NOT NULL DEFAULT '{}'")
            self._ensure_column(conn, "reviewer_runs", "duration_seconds", "REAL")
            self._ensure_column(conn, "reviewer_runs", "started_at", "TEXT")
            self._ensure_column(conn, "reviewer_runs", "finished_at", "TEXT")
            self._ensure_column(conn, "reviewer_runs", "model_id", "TEXT")
            self._ensure_column(conn, "feedback_sessions", "feedback_text", "TEXT")
            self._ensure_column(conn, "feedback_sessions", "model_reply", "TEXT")
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_cr_tasks_queue_claim
                ON cr_tasks(status, not_before_at, priority, created_at)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_cr_tasks_lease
                ON cr_tasks(status, lease_expires_at)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_reviewer_runs_current
                ON reviewer_runs(task_id, reviewer, workflow_run_id, status, attempt)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_task_events_task_created
                ON task_events(task_id, created_at)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_task_controls_pending
                ON task_controls(task_id, acknowledged_at, id)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_findings_task_status
                ON findings(task_id, status)
                """
            )
            if not conn.execute("SELECT 1 FROM schema_version LIMIT 1").fetchone():
                conn.execute("INSERT INTO schema_version(version) VALUES (1)")

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def create_task(
        self,
        *,
        app_name: str,
        repo_url: str,
        branch: str,
        task_id: Optional[str] = None,
        commit_id: Optional[str] = None,
        priority: int = 100,
        status: str = "queued",
        request_json: Optional[Dict[str, Any]] = None,
    ) -> str:
        now = now_iso()
        task_id = task_id or uuid.uuid4().hex
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO cr_tasks(
                    task_id, app_name, repo_url, branch, commit_id, status, priority,
                    queued_at, request_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    app_name,
                    repo_url,
                    branch,
                    commit_id,
                    status,
                    priority,
                    now,
                    json_dumps(request_json or {}),
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO task_events(task_id, event_type, severity, stage, message, created_at)
                VALUES (?, 'task_created', 'info', 'queued', 'Task queued', ?)
                """,
                (task_id, now),
            )
        return task_id

    def get_task(self, task_id: str) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM cr_tasks WHERE task_id=?", (task_id,)).fetchone()

    def update_task_commit_id(self, task_id: str, commit_id: str) -> None:
        now = now_iso()
        with self.transaction() as conn:
            row = conn.execute("SELECT request_json FROM cr_tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None:
                return
            try:
                request_json = json.loads(row["request_json"] or "{}")
            except json.JSONDecodeError:
                request_json = {}
            if not request_json.get("commit_id"):
                request_json["commit_id"] = commit_id
            conn.execute(
                """
                UPDATE cr_tasks
                SET commit_id=?,
                    request_json=?,
                    updated_at=?
                WHERE task_id=?
                """,
                (commit_id, json_dumps(request_json), now, task_id),
            )
            conn.execute(
                """
                INSERT INTO task_events(task_id, event_type, severity, stage, message, payload_json, created_at)
                VALUES (?, 'commit_resolved', 'info', 'prepare_repo', 'resolved checkout commit', ?, ?)
                """,
                (task_id, json_dumps({"commit_id": commit_id}), now),
            )

    def update_task_outcome(
        self,
        task_id: str,
        *,
        status: str,
        gate_status: str,
        report_url: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        now = now_iso()
        with self.transaction() as conn:
            task = conn.execute("SELECT request_json FROM cr_tasks WHERE task_id=?", (task_id,)).fetchone()
            callback_state = "retrying" if self._request_has_callback_target(task["request_json"] if task else "{}") else "skipped"
            callback_next_retry_at = now if callback_state == "retrying" else None
            conn.execute(
                """
                UPDATE cr_tasks
                SET status=?,
                    gate_status=?,
                    report_url=COALESCE(?, report_url),
                    error=?,
                    callback_state=?,
                    callback_attempts=0,
                    callback_next_retry_at=?,
                    callback_last_error=NULL,
                    finished_at=?,
                    updated_at=?
                WHERE task_id=?
                """,
                (status, gate_status, report_url, error, callback_state, callback_next_retry_at, now, now, task_id),
            )
            conn.execute(
                """
                INSERT INTO task_events(task_id, event_type, severity, stage, message, created_at)
                VALUES (?, 'task_terminal', ?, 'finalize_task', ?, ?)
                """,
                (task_id, "error" if status == "failed" else "info", f"{status}/{gate_status}", now),
            )
            if callback_state == "skipped":
                conn.execute(
                    """
                    INSERT INTO task_events(task_id, event_type, severity, stage, message, created_at)
                    VALUES (?, 'callback_skipped', 'info', 'callback', 'no callback target configured', ?)
                    """,
                    (task_id, now),
                )

    @staticmethod
    def _request_has_callback_target(request_json: str) -> bool:
        try:
            request = json.loads(request_json or "{}")
        except json.JSONDecodeError:
            return False
        return bool(request.get("callback_url") or (request.get("ci_task_id") and request.get("ci_record_id")))

    def claim_next_task(self, *, daemon_id: str, lease_seconds: int) -> Optional[sqlite3.Row]:
        now_dt = datetime.now(timezone.utc).replace(microsecond=0)
        now = now_dt.isoformat()
        lease_expires_at = (now_dt + timedelta(seconds=lease_seconds)).isoformat()
        with self.transaction() as conn:
            row = conn.execute(
                """
                SELECT task_id FROM cr_tasks
                WHERE status='queued'
                  AND (not_before_at IS NULL OR not_before_at <= ?)
                ORDER BY priority DESC, created_at ASC
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                """
                UPDATE cr_tasks
                SET status='running',
                    claimed_by=?,
                    lease_expires_at=?,
                    last_heartbeat_at=?,
                    started_at=COALESCE(started_at, ?),
                    attempts=attempts + 1,
                    updated_at=?
                WHERE task_id=? AND status='queued'
                """,
                (daemon_id, lease_expires_at, now, now, now, row["task_id"]),
            )
            if conn.total_changes < 1:
                return None
            conn.execute(
                """
                INSERT INTO task_events(task_id, event_type, severity, stage, message, created_at)
                VALUES (?, 'task_claimed', 'info', 'running', ?, ?)
                """,
                (row["task_id"], f"Claimed by {daemon_id}", now),
            )
            return conn.execute("SELECT * FROM cr_tasks WHERE task_id=?", (row["task_id"],)).fetchone()

    def create_reviewer_run(
        self,
        *,
        task_id: str,
        reviewer: str,
        workflow_run_id: str,
        attempt: int,
        status: str,
        session_id: Optional[str] = None,
        model_id: Optional[str] = None,
        raw_log_path: Optional[str] = None,
        output_path: Optional[str] = None,
        error: Optional[str] = None,
        duration_seconds: Optional[float] = None,
        token_usage: Optional[Dict[str, Any]] = None,
    ) -> int:
        now = now_iso()
        usage = token_usage or {}
        started_at = now if status in {"running", "success", "failed", "cancelled"} else None
        finished_at = now if status in {"success", "failed", "cancelled"} else None
        with self.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO reviewer_runs(
                    task_id, reviewer, workflow_run_id, attempt, status, session_id, model_id,
                    raw_log_path, output_path, error, duration_seconds,
                    input_tokens, cache_read_tokens, cache_write_tokens, output_tokens,
                    reasoning_tokens, total_tokens, cost_usd, created_at, updated_at,
                    started_at, finished_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    reviewer,
                    workflow_run_id,
                    attempt,
                    status,
                    session_id,
                    model_id,
                    raw_log_path,
                    output_path,
                    error,
                    duration_seconds,
                    int(usage.get("input_tokens") or 0),
                    int(usage.get("cache_read_tokens") or 0),
                    int(usage.get("cache_write_tokens") or 0),
                    int(usage.get("output_tokens") or 0),
                    int(usage.get("reasoning_tokens") or 0),
                    int(usage.get("total_tokens") or 0),
                    float(usage.get("cost_usd") or 0),
                    now,
                    now,
                    started_at,
                    finished_at,
                ),
            )
            return int(cursor.lastrowid)

    def start_reviewer_run(
        self,
        *,
        task_id: str,
        reviewer: str,
        workflow_run_id: str,
        attempt: int,
        model_id: Optional[str] = None,
    ) -> int:
        now = now_iso()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO reviewer_runs(
                    task_id, reviewer, workflow_run_id, attempt, status, model_id,
                    created_at, updated_at, started_at
                )
                VALUES (?, ?, ?, ?, 'running', ?, ?, ?, ?)
                ON CONFLICT(task_id, reviewer, workflow_run_id, attempt) DO UPDATE SET
                    status='running',
                    error=NULL,
                    model_id=COALESCE(excluded.model_id, reviewer_runs.model_id),
                    updated_at=excluded.updated_at,
                    started_at=COALESCE(reviewer_runs.started_at, excluded.started_at),
                    finished_at=NULL
                """,
                (task_id, reviewer, workflow_run_id, attempt, model_id, now, now, now),
            )
            row = conn.execute(
                """
                SELECT id FROM reviewer_runs
                WHERE task_id=? AND reviewer=? AND workflow_run_id=? AND attempt=?
                """,
                (task_id, reviewer, workflow_run_id, attempt),
            ).fetchone()
            return int(row["id"])

    def finish_reviewer_run(
        self,
        run_id: int,
        *,
        status: str,
        session_id: Optional[str] = None,
        raw_log_path: Optional[str] = None,
        output_path: Optional[str] = None,
        error: Optional[str] = None,
        model_id: Optional[str] = None,
        token_usage: Optional[Dict[str, Any]] = None,
    ) -> None:
        now = now_iso()
        usage = token_usage or {}
        with self.transaction() as conn:
            row = conn.execute("SELECT started_at FROM reviewer_runs WHERE id=?", (run_id,)).fetchone()
            duration_seconds = None
            if row and row["started_at"]:
                try:
                    duration_seconds = (
                        datetime.fromisoformat(now) - datetime.fromisoformat(str(row["started_at"]))
                    ).total_seconds()
                except ValueError:
                    duration_seconds = None
            conn.execute(
                """
                UPDATE reviewer_runs
                SET status=?,
                    session_id=?,
                    raw_log_path=?,
                    output_path=?,
                    error=?,
                    model_id=COALESCE(?, model_id),
                    duration_seconds=?,
                    input_tokens=?,
                    cache_read_tokens=?,
                    cache_write_tokens=?,
                    output_tokens=?,
                    reasoning_tokens=?,
                    total_tokens=?,
                    cost_usd=?,
                    updated_at=?,
                    finished_at=?
                WHERE id=?
                """,
                (
                    status,
                    session_id,
                    raw_log_path,
                    output_path,
                    error,
                    model_id,
                    duration_seconds,
                    int(usage.get("input_tokens") or 0),
                    int(usage.get("cache_read_tokens") or 0),
                    int(usage.get("cache_write_tokens") or 0),
                    int(usage.get("output_tokens") or 0),
                    int(usage.get("reasoning_tokens") or 0),
                    int(usage.get("total_tokens") or 0),
                    float(usage.get("cost_usd") or 0),
                    now,
                    now,
                    run_id,
                ),
            )

    def create_reviewer_plan(
        self,
        *,
        task_id: str,
        workflow_run_id: str,
        reviewer: str,
        required: bool,
        risk_tier: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> int:
        now = now_iso()
        with self.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO reviewer_plans(
                    task_id, workflow_run_id, reviewer, required, risk_tier, reason, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id, workflow_run_id, reviewer) DO UPDATE SET
                    required=excluded.required,
                    risk_tier=excluded.risk_tier,
                    reason=excluded.reason
                """,
                (task_id, workflow_run_id, reviewer, 1 if required else 0, risk_tier, reason, now),
            )
            if cursor.lastrowid:
                return int(cursor.lastrowid)
            row = conn.execute(
                """
                SELECT id FROM reviewer_plans
                WHERE task_id=? AND workflow_run_id=? AND reviewer=?
                """,
                (task_id, workflow_run_id, reviewer),
            ).fetchone()
            return int(row["id"])

    def current_successful_reviewer_runs(self, task_id: str) -> List[sqlite3.Row]:
        with self.connect() as conn:
            rows = list(
                conn.execute(
                    """
                    SELECT * FROM reviewer_runs
                    WHERE task_id=? AND status='success'
                    ORDER BY reviewer ASC, workflow_run_id DESC, attempt DESC
                    """,
                    (task_id,),
                )
            )
        selected: Dict[str, sqlite3.Row] = {}
        for row in rows:
            selected.setdefault(row["reviewer"], row)
        return list(selected.values())

    def create_finding(
        self,
        *,
        task_id: str,
        reviewer_run_id: Optional[int],
        file_path: str,
        line: Optional[int],
        severity: str,
        title: str,
        detail: str,
        suggestion: Optional[str] = None,
        finding_id: Optional[str] = None,
    ) -> str:
        now = now_iso()
        finding_id = finding_id or f"fnd_{uuid.uuid4().hex}"
        normalized = normalize_severity(severity).value
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO findings(
                    finding_id, task_id, reviewer_run_id, file_path, line, severity,
                    title, detail, suggestion, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    finding_id,
                    task_id,
                    reviewer_run_id,
                    file_path,
                    line,
                    normalized,
                    title,
                    detail,
                    suggestion,
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO finding_events(finding_id, task_id, event_type, payload_json, created_at)
                VALUES (?, ?, 'finding_created', ?, ?)
                """,
                (finding_id, task_id, json_dumps({"severity": normalized}), now),
            )
        return finding_id

    def get_finding(self, *, task_id: str, finding_id: str) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM findings WHERE task_id=? AND finding_id=?",
                (task_id, finding_id),
            ).fetchone()

    def get_finding_with_reviewer(self, *, task_id: str, finding_id: str) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT
                    f.*,
                    f.reviewer_run_id AS source_reviewer_run_id,
                    rr.reviewer AS source_reviewer,
                    rr.session_id AS source_session_id
                FROM findings f
                LEFT JOIN reviewer_runs rr ON rr.id = f.reviewer_run_id
                WHERE f.task_id=? AND f.finding_id=?
                """,
                (task_id, finding_id),
            ).fetchone()

    def add_task_event(
        self,
        task_id: Optional[str],
        event_type: str,
        message: str,
        *,
        stage: Optional[str] = None,
        severity: str = "info",
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        now = now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO task_events(task_id, event_type, severity, stage, message, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (task_id, event_type, severity, stage, message, json_dumps(payload), now),
            )

    def request_task_stop(self, task_id: str, *, reason: Optional[str] = None) -> None:
        self._request_task_control(task_id, "stop", reason=reason)
        now = now_iso()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO task_events(task_id, event_type, severity, stage, message, payload_json, created_at)
                VALUES (?, 'task_stop_requested', 'warn', 'control', ?, '{}', ?)
                """,
                (task_id, reason or "stop requested", now),
            )

    def cancel_task(self, task_id: str, *, reason: Optional[str] = None) -> None:
        self._request_task_control(task_id, "cancel", reason=reason)
        now = now_iso()
        message = reason or "cancelled by operator"
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE cr_tasks
                SET status='cancelled',
                    gate_status='cancelled',
                    claimed_by=NULL,
                    lease_expires_at=NULL,
                    error=?,
                    finished_at=COALESCE(finished_at, ?),
                    updated_at=?
                WHERE task_id=?
                """,
                (message, now, now, task_id),
            )
            conn.execute(
                """
                UPDATE reviewer_runs
                SET status='cancelled',
                    error=COALESCE(error, ?),
                    finished_at=COALESCE(finished_at, ?),
                    updated_at=?
                WHERE task_id=? AND status IN ('queued', 'running')
                """,
                (message, now, now, task_id),
            )
            conn.execute(
                """
                UPDATE task_controls
                SET acknowledged_at=COALESCE(acknowledged_at, ?)
                WHERE task_id=?
                """,
                (now, task_id),
            )
            conn.execute(
                """
                INSERT INTO task_events(task_id, event_type, severity, stage, message, payload_json, created_at)
                VALUES (?, 'task_cancelled', 'warn', 'cancel', ?, '{}', ?)
                """,
                (task_id, message, now),
            )

    def requeue_task(self, task_id: str, *, reason: Optional[str] = None) -> None:
        now = now_iso()
        message = reason or "requeued by operator"
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE cr_tasks
                SET status='queued',
                    gate_status=NULL,
                    claimed_by=NULL,
                    lease_expires_at=NULL,
                    finished_at=NULL,
                    error=NULL,
                    updated_at=?,
                    queued_at=COALESCE(queued_at, ?)
                WHERE task_id=?
                """,
                (now, now, task_id),
            )
            conn.execute(
                """
                UPDATE reviewer_runs
                SET status='queued',
                    session_id=NULL,
                    raw_log_path=NULL,
                    output_path=NULL,
                    error=NULL,
                    duration_seconds=NULL,
                    input_tokens=0,
                    cache_read_tokens=0,
                    cache_write_tokens=0,
                    output_tokens=0,
                    reasoning_tokens=0,
                    total_tokens=0,
                    cost_usd=0,
                    started_at=NULL,
                    finished_at=NULL,
                    updated_at=?
                WHERE task_id=? AND status IN ('queued', 'running', 'failed', 'cancelled')
                """,
                (now, task_id),
            )
            conn.execute(
                """
                UPDATE task_controls
                SET acknowledged_at=COALESCE(acknowledged_at, ?)
                WHERE task_id=?
                """,
                (now, task_id),
            )
            conn.execute(
                """
                INSERT INTO task_events(task_id, event_type, severity, stage, message, payload_json, created_at)
                VALUES (?, 'task_requeued', 'warn', 'queued', ?, '{}', ?)
                """,
                (task_id, message, now),
            )

    def mark_task_stopped(self, task_id: str, *, reason: Optional[str] = None, stage: str = "stopped") -> None:
        now = now_iso()
        message = reason or "task stopped"
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE cr_tasks
                SET status='cancelled',
                    gate_status='stopped',
                    claimed_by=NULL,
                    lease_expires_at=NULL,
                    error=?,
                    finished_at=COALESCE(finished_at, ?),
                    updated_at=?
                WHERE task_id=?
                """,
                (message, now, now, task_id),
            )
            conn.execute(
                """
                UPDATE reviewer_runs
                SET status='cancelled',
                    error=COALESCE(error, ?),
                    finished_at=COALESCE(finished_at, ?),
                    updated_at=?
                WHERE task_id=? AND status IN ('queued', 'running')
                """,
                (message, now, now, task_id),
            )
            conn.execute(
                """
                UPDATE task_controls
                SET acknowledged_at=COALESCE(acknowledged_at, ?)
                WHERE task_id=? AND action='stop'
                """,
                (now, task_id),
            )
            conn.execute(
                """
                INSERT INTO task_events(task_id, event_type, severity, stage, message, payload_json, created_at)
                VALUES (?, 'task_stopped', 'warn', ?, ?, '{}', ?)
                """,
                (task_id, stage, message, now),
            )

    def check_task_control(self, task_id: str) -> Optional[tuple[str, str]]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT action, reason FROM task_controls
                WHERE task_id=? AND acknowledged_at IS NULL
                ORDER BY id DESC
                LIMIT 1
                """,
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        return row["action"], row["reason"] or f"{row['action']} requested"

    def _request_task_control(self, task_id: str, action: str, *, reason: Optional[str] = None) -> None:
        now = now_iso()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO task_controls(task_id, action, reason, requested_at)
                VALUES (?, ?, ?, ?)
                """,
                (task_id, action, reason, now),
            )

    def upsert_heartbeat(
        self,
        *,
        runner_id: str,
        task_id: Optional[str],
        status: str,
        message: Optional[str] = None,
        pid: Optional[int] = None,
        hostname: Optional[str] = None,
        loaded_config_hash: Optional[str] = None,
    ) -> None:
        now = now_iso()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO runner_heartbeats(
                    runner_id, task_id, pid, hostname, status, message,
                    started_at, heartbeat_at, loaded_config_hash, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(runner_id) DO UPDATE SET
                    task_id=excluded.task_id,
                    pid=excluded.pid,
                    hostname=excluded.hostname,
                    status=excluded.status,
                    message=excluded.message,
                    heartbeat_at=excluded.heartbeat_at,
                    loaded_config_hash=excluded.loaded_config_hash,
                    updated_at=excluded.updated_at
                """,
                (
                    runner_id,
                    task_id,
                    pid,
                    hostname,
                    status,
                    message,
                    now,
                    now,
                    loaded_config_hash,
                    now,
                    now,
                ),
            )

    def create_feedback_session(
        self,
        *,
        task_id: str,
        finding_id: Optional[str],
        status: str,
        opencode_session_id: Optional[str] = None,
        parent_reviewer_run_id: Optional[int] = None,
        feedback_text: Optional[str] = None,
        model_reply: Optional[str] = None,
        token_usage: Optional[Dict[str, Any]] = None,
        feedback_session_id: Optional[str] = None,
    ) -> str:
        now = now_iso()
        usage = token_usage or {}
        feedback_session_id = feedback_session_id or f"fb_{uuid.uuid4().hex}"
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO feedback_sessions(
                    feedback_session_id, task_id, finding_id, parent_reviewer_run_id,
                    opencode_session_id, feedback_text, model_reply, status, input_tokens, cache_read_tokens,
                    cache_write_tokens, output_tokens, reasoning_tokens, total_tokens,
                    cost_usd, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    feedback_session_id,
                    task_id,
                    finding_id,
                    parent_reviewer_run_id,
                    opencode_session_id,
                    feedback_text,
                    model_reply,
                    status,
                    int(usage.get("input_tokens") or 0),
                    int(usage.get("cache_read_tokens") or 0),
                    int(usage.get("cache_write_tokens") or 0),
                    int(usage.get("output_tokens") or 0),
                    int(usage.get("reasoning_tokens") or 0),
                    int(usage.get("total_tokens") or 0),
                    float(usage.get("cost_usd") or 0),
                    now,
                    now,
                ),
            )
        return feedback_session_id

    def finish_feedback_session(
        self,
        feedback_session_id: str,
        *,
        status: str,
        opencode_session_id: Optional[str] = None,
        model_reply: Optional[str] = None,
        token_usage: Optional[Dict[str, Any]] = None,
    ) -> None:
        now = now_iso()
        usage = token_usage or {}
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE feedback_sessions
                SET status=?,
                    opencode_session_id=?,
                    model_reply=?,
                    input_tokens=?,
                    cache_read_tokens=?,
                    cache_write_tokens=?,
                    output_tokens=?,
                    reasoning_tokens=?,
                    total_tokens=?,
                    cost_usd=?,
                    updated_at=?
                WHERE feedback_session_id=?
                """,
                (
                    status,
                    opencode_session_id,
                    model_reply,
                    int(usage.get("input_tokens") or 0),
                    int(usage.get("cache_read_tokens") or 0),
                    int(usage.get("cache_write_tokens") or 0),
                    int(usage.get("output_tokens") or 0),
                    int(usage.get("reasoning_tokens") or 0),
                    int(usage.get("total_tokens") or 0),
                    float(usage.get("cost_usd") or 0),
                    now,
                    feedback_session_id,
                ),
            )

    def update_finding_status(
        self,
        *,
        task_id: str,
        finding_id: str,
        status: str,
        actor: Optional[str] = None,
        message: Optional[str] = None,
    ) -> None:
        now = now_iso()
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE findings SET status=?, updated_at=? WHERE finding_id=? AND task_id=?",
                (status, now, finding_id, task_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"finding not found for task: {finding_id}")
            conn.execute(
                """
                INSERT INTO finding_events(finding_id, task_id, event_type, actor, message, created_at)
                VALUES (?, ?, 'finding_status_changed', ?, ?, ?)
                """,
                (finding_id, task_id, actor, message, now),
            )

    def open_findings_count(self, task_id: str) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM findings WHERE task_id=? AND status='open'",
                (task_id,),
            ).fetchone()
        return int(row["n"] or 0)

    def set_callback_state(
        self,
        task_id: str,
        *,
        state: str,
        attempts: int,
        next_retry_at: Optional[str] = None,
        last_error: Optional[str] = None,
        payload_digest: Optional[str] = None,
        history: Optional[list[Dict[str, Any]]] = None,
    ) -> None:
        now = now_iso()
        history_json = json_dumps(history) if history is not None else None
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE cr_tasks
                SET callback_state=?,
                    callback_attempts=?,
                    callback_next_retry_at=?,
                    callback_last_error=?,
                    callback_payload_digest=COALESCE(?, callback_payload_digest),
                    callback_history_json=COALESCE(?, callback_history_json),
                    updated_at=?
                WHERE task_id=?
                """,
                (state, attempts, next_retry_at, last_error, payload_digest, history_json, now, task_id),
            )

    def recover_stale_running_tasks(self, *, max_attempts: int = 3) -> List[str]:
        now = now_iso()
        recovered: List[str] = []
        with self.transaction() as conn:
            rows = list(
                conn.execute(
                    """
                    SELECT task_id, attempts FROM cr_tasks
                    WHERE status='running'
                      AND lease_expires_at IS NOT NULL
                      AND lease_expires_at < ?
                    ORDER BY updated_at ASC
                    """,
                    (now,),
                )
            )
            for row in rows:
                task_id = row["task_id"]
                if int(row["attempts"] or 0) >= max_attempts:
                    conn.execute(
                        """
                        UPDATE cr_tasks
                        SET status='failed',
                            gate_status='incomplete',
                            claimed_by=NULL,
                            lease_expires_at=NULL,
                            error='stale task retry budget exhausted',
                            updated_at=?
                        WHERE task_id=?
                        """,
                        (now, task_id),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE cr_tasks
                        SET status='queued',
                            claimed_by=NULL,
                            lease_expires_at=NULL,
                            updated_at=?
                        WHERE task_id=?
                        """,
                        (now, task_id),
                    )
                    recovered.append(task_id)
                conn.execute(
                    """
                    INSERT INTO task_events(task_id, event_type, severity, stage, message, created_at)
                    VALUES (?, 'task_recovered', 'warn', 'recover_stale', 'stale running task recovered', ?)
                    """,
                    (task_id, now),
                )
        return recovered

    def due_callback_tasks(self, *, limit: int = 20) -> List[str]:
        now = now_iso()
        with self.connect() as conn:
            return [
                row["task_id"]
                for row in conn.execute(
                    """
                    SELECT task_id FROM cr_tasks
                    WHERE callback_state='retrying'
                      AND (callback_next_retry_at IS NULL OR callback_next_retry_at <= ?)
                    ORDER BY callback_next_retry_at ASC, updated_at ASC
                    LIMIT ?
                    """,
                    (now, limit),
                )
            ]

    def task_counts(self) -> Dict[str, int]:
        with self.connect() as conn:
            return {row["status"]: int(row["n"]) for row in conn.execute("SELECT status, COUNT(*) AS n FROM cr_tasks GROUP BY status")}

    def latest_heartbeats(self, *, limit: int = 5) -> List[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    "SELECT * FROM runner_heartbeats ORDER BY updated_at DESC LIMIT ?",
                    (limit,),
                )
            )

    def active_tasks(self, *, limit: int = 10) -> List[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT * FROM cr_tasks
                    WHERE status IN ('queued', 'running')
                    ORDER BY priority DESC, updated_at ASC
                    LIMIT ?
                    """,
                    (limit,),
                )
            )

    def aggregate_task_usage(self, task_id: str) -> Dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COALESCE(SUM(input_tokens), 0) AS input_tokens,
                    COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                    COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens,
                    COALESCE(SUM(output_tokens), 0) AS output_tokens,
                    COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens,
                    COALESCE(SUM(total_tokens), 0) AS total_tokens,
                    COALESCE(SUM(cost_usd), 0) AS cost_usd
                FROM (
                    SELECT input_tokens, cache_read_tokens, cache_write_tokens, output_tokens, reasoning_tokens, total_tokens, cost_usd
                    FROM reviewer_runs
                    WHERE task_id=?
                    UNION ALL
                    SELECT input_tokens, cache_read_tokens, cache_write_tokens, output_tokens, reasoning_tokens, total_tokens, cost_usd
                    FROM feedback_sessions
                    WHERE task_id=?
                )
                """,
                (task_id, task_id),
            ).fetchone()
        return {
            "input_tokens": int(row["input_tokens"] or 0),
            "cache_read_tokens": int(row["cache_read_tokens"] or 0),
            "cache_write_tokens": int(row["cache_write_tokens"] or 0),
            "output_tokens": int(row["output_tokens"] or 0),
            "reasoning_tokens": int(row["reasoning_tokens"] or 0),
            "total_tokens": int(row["total_tokens"] or 0),
            "cost_usd": float(row["cost_usd"] or 0),
        }

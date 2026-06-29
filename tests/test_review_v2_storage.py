import sqlite3
from pathlib import Path

import pytest

from cr_agent.review_v2.storage import ReviewDB


def test_review_db_initializes_schema_pragmas_and_indexes(tmp_path: Path) -> None:
    db = ReviewDB(tmp_path / "review.sqlite3")
    db.init()
    db.init()

    with db.connect() as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {
            "schema_version",
            "cr_tasks",
            "reviewer_plans",
            "reviewer_runs",
            "findings",
            "finding_events",
            "feedback_sessions",
            "task_events",
            "task_controls",
            "runner_heartbeats",
        }.issubset(tables)
        indexes = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        assert "idx_cr_tasks_queue_claim" in indexes
        assert "idx_reviewer_runs_current" in indexes
        assert "idx_task_events_task_created" in indexes
        task_columns = {row["name"] for row in conn.execute("PRAGMA table_info(cr_tasks)")}
        assert {"request_json", "callback_payload_digest", "callback_history_json"}.issubset(task_columns)


def test_transaction_rolls_back_on_error(tmp_path: Path) -> None:
    db = ReviewDB(tmp_path / "review.sqlite3")
    db.init()

    with pytest.raises(RuntimeError):
        with db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO cr_tasks(task_id, app_name, repo_url, branch, status, created_at, updated_at)
                VALUES ('t1', 'demo', 'git@github.com:comain/code-review-agent.git', 'feature/a', 'queued', 'now', 'now')
                """
            )
            raise RuntimeError("boom")

    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM cr_tasks").fetchone()[0] == 0


def test_create_and_claim_task_is_atomic_and_lease_based(tmp_path: Path) -> None:
    db = ReviewDB(tmp_path / "review.sqlite3")
    db.init()
    task_id = db.create_task(
        task_id="task1",
        app_name="demo",
        repo_url="git@github.com:comain/code-review-agent.git",
        branch="feature/a",
        priority=10,
    )

    claimed = db.claim_next_task(daemon_id="daemon-a", lease_seconds=60)
    second_claim = db.claim_next_task(daemon_id="daemon-b", lease_seconds=60)

    assert claimed is not None
    assert claimed["task_id"] == task_id
    assert claimed["status"] == "running"
    assert claimed["claimed_by"] == "daemon-a"
    assert claimed["attempts"] == 1
    assert second_claim is None


def test_task_control_stop_cancel_and_requeue(tmp_path: Path) -> None:
    db = ReviewDB(tmp_path / "review.sqlite3")
    db.init()
    task_id = db.create_task(task_id="task1", app_name="demo", repo_url="git@github.com:comain/code-review-agent.git", branch="feature/a")
    db.claim_next_task(daemon_id="daemon-a", lease_seconds=60)
    db.start_reviewer_run(task_id=task_id, reviewer="correctness", workflow_run_id="wf1", attempt=1)

    db.request_task_stop(task_id, reason="operator stop")

    assert db.check_task_control(task_id) == ("stop", "operator stop")

    db.cancel_task(task_id, reason="operator cancel")
    with db.connect() as conn:
        task = conn.execute("SELECT status, gate_status, error, claimed_by FROM cr_tasks WHERE task_id=?", (task_id,)).fetchone()
        run = conn.execute("SELECT status, error FROM reviewer_runs WHERE task_id=?", (task_id,)).fetchone()
    assert task["status"] == "cancelled"
    assert task["gate_status"] == "cancelled"
    assert task["error"] == "operator cancel"
    assert task["claimed_by"] is None
    assert run["status"] == "cancelled"

    db.requeue_task(task_id, reason="retry after cancel")
    with db.connect() as conn:
        task = conn.execute("SELECT status, gate_status, claimed_by, lease_expires_at, error FROM cr_tasks WHERE task_id=?", (task_id,)).fetchone()
        controls = list(conn.execute("SELECT acknowledged_at FROM task_controls WHERE task_id=?", (task_id,)))
    assert task["status"] == "queued"
    assert task["gate_status"] is None
    assert task["claimed_by"] is None
    assert task["lease_expires_at"] is None
    assert task["error"] is None
    assert all(row["acknowledged_at"] for row in controls)


def test_reviewer_attempts_and_current_successful_run(tmp_path: Path) -> None:
    db = ReviewDB(tmp_path / "review.sqlite3")
    db.init()
    task_id = db.create_task(
        task_id="task1",
        app_name="demo",
        repo_url="git@github.com:comain/code-review-agent.git",
        branch="feature/a",
    )
    db.create_reviewer_run(
        task_id=task_id,
        reviewer="correctness_light",
        workflow_run_id="wf1",
        attempt=1,
        status="failed",
        session_id="ses_old",
    )
    db.create_reviewer_run(
        task_id=task_id,
        reviewer="correctness_light",
        workflow_run_id="wf1",
        attempt=2,
        status="success",
        session_id="ses_new",
        token_usage={
            "input_tokens": 100,
            "cache_read_tokens": 20,
            "cache_write_tokens": 5,
            "output_tokens": 30,
            "reasoning_tokens": 7,
            "total_tokens": 162,
            "cost_usd": 0.1234,
        },
    )

    runs = db.current_successful_reviewer_runs(task_id)
    usage = db.aggregate_task_usage(task_id)

    assert [run["session_id"] for run in runs] == ["ses_new"]
    assert usage["input_tokens"] == 100
    assert usage["cache_read_tokens"] == 20
    assert usage["cache_write_tokens"] == 5
    assert usage["output_tokens"] == 30
    assert usage["reasoning_tokens"] == 7
    assert usage["total_tokens"] == 162
    assert usage["cost_usd"] == 0.1234


def test_findings_normalize_critical_severity_to_fatal(tmp_path: Path) -> None:
    db = ReviewDB(tmp_path / "review.sqlite3")
    db.init()
    task_id = db.create_task(
        task_id="task1",
        app_name="demo",
        repo_url="git@github.com:comain/code-review-agent.git",
        branch="feature/a",
    )
    run_id = db.create_reviewer_run(
        task_id=task_id,
        reviewer="security",
        workflow_run_id="wf1",
        attempt=1,
        status="success",
        session_id="ses_security",
    )

    finding_id = db.create_finding(
        task_id=task_id,
        reviewer_run_id=run_id,
        file_path="src/app.py",
        line=12,
        severity="critical",
        title="unsafe callback",
        detail="detail",
    )

    with db.connect() as conn:
        row = conn.execute("SELECT * FROM findings WHERE finding_id=?", (finding_id,)).fetchone()
        assert row["severity"] == "fatal"


def test_foreign_keys_reject_orphan_reviewer_run(tmp_path: Path) -> None:
    db = ReviewDB(tmp_path / "review.sqlite3")
    db.init()

    with pytest.raises(sqlite3.IntegrityError):
        db.create_reviewer_run(
            task_id="missing",
            reviewer="correctness_light",
            workflow_run_id="wf1",
            attempt=1,
            status="success",
        )


def test_reviewer_plan_events_heartbeat_feedback_and_callback_state(tmp_path: Path) -> None:
    db = ReviewDB(tmp_path / "review.sqlite3")
    db.init()
    task_id = db.create_task(
        task_id="task1",
        app_name="demo",
        repo_url="git@github.com:comain/code-review-agent.git",
        branch="feature/a",
    )
    db.create_reviewer_plan(
        task_id=task_id,
        workflow_run_id="wf1",
        reviewer="security",
        required=True,
        risk_tier="high",
        reason="security-sensitive path",
    )
    db.add_task_event(task_id, "stage_started", "running reviewer", stage="run_reviewers")
    db.upsert_heartbeat(
        runner_id="daemon-a",
        task_id=task_id,
        status="RUNNING",
        message="reviewing",
        pid=123,
        hostname="production host",
        loaded_config_hash="abcd",
    )
    feedback_id = db.create_feedback_session(
        task_id=task_id,
        finding_id=None,
        status="queued",
        opencode_session_id="ses_feedback",
        model_reply="模型回复",
        token_usage={"total_tokens": 10, "cost_usd": 0.01},
    )
    db.set_callback_state(
        task_id,
        state="retrying",
        attempts=2,
        next_retry_at="2026-06-24T10:00:00+00:00",
        last_error="timeout",
        payload_digest="digest1",
        history=[{"attempt": 1}],
    )

    with db.connect() as conn:
        plan = conn.execute("SELECT * FROM reviewer_plans WHERE task_id=?", (task_id,)).fetchone()
        event = conn.execute("SELECT * FROM task_events WHERE event_type='stage_started'").fetchone()
        heartbeat = conn.execute("SELECT * FROM runner_heartbeats WHERE runner_id='daemon-a'").fetchone()
        feedback = conn.execute("SELECT * FROM feedback_sessions WHERE feedback_session_id=?", (feedback_id,)).fetchone()
        task = conn.execute("SELECT * FROM cr_tasks WHERE task_id=?", (task_id,)).fetchone()

    assert plan["reviewer"] == "security"
    assert plan["required"] == 1
    assert event["stage"] == "run_reviewers"
    assert heartbeat["task_id"] == task_id
    assert heartbeat["loaded_config_hash"] == "abcd"
    assert feedback["opencode_session_id"] == "ses_feedback"
    assert feedback["model_reply"] == "模型回复"
    assert feedback["total_tokens"] == 10
    assert feedback["cost_usd"] == 0.01
    assert task["callback_state"] == "retrying"
    assert task["callback_attempts"] == 2
    assert task["callback_last_error"] == "timeout"
    assert task["callback_payload_digest"] == "digest1"
    assert task["callback_history_json"] == '[{"attempt":1}]'


def test_create_task_persists_original_request_json(tmp_path: Path) -> None:
    db = ReviewDB(tmp_path / "review.sqlite3")
    db.init()

    task_id = db.create_task(
        task_id="task1",
        app_name="demo",
        repo_url="git@github.com:comain/code-review-agent.git",
        branch="feature/a",
        request_json={"callback_url": "http://127.0.0.1:9999/callback", "metadata": {"pipeline": "p1"}},
    )

    with db.connect() as conn:
        task = conn.execute("SELECT request_json FROM cr_tasks WHERE task_id=?", (task_id,)).fetchone()

    assert '"callback_url":"http://127.0.0.1:9999/callback"' in task["request_json"]

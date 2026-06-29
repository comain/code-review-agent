from pathlib import Path
from datetime import datetime
import json
from zoneinfo import ZoneInfo

from cr_agent.config import Settings
from cr_agent.review_v2.feedback_patterns import FeedbackPatternSyncResult
from cr_agent.review_v2.daemon import CRReviewDaemon
from cr_agent.review_v2.storage import ReviewDB


class RecordingRunner:
    def __init__(self):
        self.task_ids = []

    def run(self, task_id: str) -> None:
        self.task_ids.append(task_id)


class RecordingCallbackClient:
    def __init__(self):
        self.calls = []

    def send(self, record, payload):
        self.calls.append((record, payload))
        return [{"attempt": 1, "status_code": 200}]


class RecordingFeedbackPatternSyncer:
    def __init__(self):
        self.calls = 0

    def __call__(self, db, settings):
        self.calls += 1
        return FeedbackPatternSyncResult(
            output_path=str(settings.review_v2_feedback_pattern_output_path or ""),
            changed=False,
            false_positive_count=1,
            accepted_feedback_count=2,
            total_count=3,
        )


def test_daemon_once_claims_single_task_and_prevents_duplicate_claim(tmp_path: Path) -> None:
    settings = Settings(base_dir=tmp_path / "runtime", review_v2_db_path=tmp_path / "review.sqlite3")
    db = ReviewDB(settings.review_v2_db_path)
    db.init()
    db.create_task(task_id="task1", app_name="demo", repo_url=str(tmp_path), branch="feature/a")
    runner_a = RecordingRunner()
    runner_b = RecordingRunner()

    assert CRReviewDaemon(settings, db=db, workflow_runner=runner_a, daemon_id="daemon-a").once() == 1
    assert CRReviewDaemon(settings, db=db, workflow_runner=runner_b, daemon_id="daemon-b").once() == 0

    assert runner_a.task_ids == ["task1"]
    assert runner_b.task_ids == []


def test_daemon_runs_feedback_pattern_sync_once_per_scheduled_day(tmp_path: Path) -> None:
    settings = Settings(
        base_dir=tmp_path / "runtime",
        review_v2_db_path=tmp_path / "review.sqlite3",
        review_v2_feedback_pattern_sync_hour=0,
        review_v2_feedback_pattern_sync_minute=0,
        review_v2_feedback_pattern_output_path=tmp_path / "patterns.md",
    )
    db = ReviewDB(settings.review_v2_db_path)
    db.init()
    syncer = RecordingFeedbackPatternSyncer()
    daemon = CRReviewDaemon(
        settings,
        db=db,
        workflow_runner=RecordingRunner(),
        daemon_id="daemon-a",
        feedback_pattern_syncer=syncer,
    )
    now = datetime(2026, 6, 25, 1, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    assert daemon.maybe_sync_feedback_patterns(now=now) is True
    assert daemon.maybe_sync_feedback_patterns(now=now) is False
    assert daemon.maybe_sync_feedback_patterns(now=now.replace(day=26)) is True
    assert syncer.calls == 2
    with db.connect() as conn:
        rows = list(
            conn.execute(
                "SELECT event_type, payload_json FROM task_events WHERE event_type='feedback_pattern_sync_completed'"
            )
        )
    assert len(rows) == 2
    assert json.loads(rows[0]["payload_json"])["total_count"] == 3


def test_daemon_recovers_stale_running_task(tmp_path: Path) -> None:
    settings = Settings(base_dir=tmp_path / "runtime", review_v2_db_path=tmp_path / "review.sqlite3")
    db = ReviewDB(settings.review_v2_db_path)
    db.init()
    db.create_task(task_id="task1", app_name="demo", repo_url=str(tmp_path), branch="feature/a")
    db.claim_next_task(daemon_id="daemon-a", lease_seconds=-1)

    recovered = CRReviewDaemon(settings, db=db, workflow_runner=RecordingRunner(), daemon_id="daemon-b").recover_stale()

    assert recovered == ["task1"]
    with db.connect() as conn:
        row = conn.execute("SELECT status, claimed_by FROM cr_tasks WHERE task_id='task1'").fetchone()
    assert row["status"] == "queued"
    assert row["claimed_by"] is None


def test_daemon_retry_callbacks_sends_due_callbacks_without_changing_review_outcome(tmp_path: Path) -> None:
    settings = Settings(base_dir=tmp_path / "runtime", review_v2_db_path=tmp_path / "review.sqlite3")
    db = ReviewDB(settings.review_v2_db_path)
    db.init()
    db.create_task(
        task_id="task1",
        app_name="demo",
        repo_url=str(tmp_path),
        branch="feature/a",
        request_json={"callback_url": "http://127.0.0.1:9999/callback"},
    )
    db.update_task_outcome("task1", status="success", gate_status="passed")
    db.set_callback_state("task1", state="retrying", attempts=1, next_retry_at="2000-01-01T00:00:00+00:00", last_error="timeout")
    callback_client = RecordingCallbackClient()

    completed = CRReviewDaemon(
        settings,
        db=db,
        workflow_runner=RecordingRunner(),
        callback_client=callback_client,
        daemon_id="daemon-a",
    ).retry_callbacks()

    assert completed == ["task1"]
    assert callback_client.calls[0][1].passed is True
    with db.connect() as conn:
        row = conn.execute(
            "SELECT status, gate_status, callback_state, callback_attempts, callback_history_json FROM cr_tasks WHERE task_id='task1'"
        ).fetchone()
    assert row["status"] == "success"
    assert row["gate_status"] == "passed"
    assert row["callback_state"] == "succeeded"
    assert row["callback_attempts"] == 2
    assert json.loads(row["callback_history_json"])[0]["status_code"] == 200


def test_daemon_retry_callbacks_skips_tasks_without_callback_target(tmp_path: Path) -> None:
    settings = Settings(base_dir=tmp_path / "runtime", review_v2_db_path=tmp_path / "review.sqlite3")
    db = ReviewDB(settings.review_v2_db_path)
    db.init()
    db.create_task(task_id="task1", app_name="demo", repo_url=str(tmp_path), branch="feature/a")
    db.update_task_outcome("task1", status="success", gate_status="passed")

    completed = CRReviewDaemon(settings, db=db, workflow_runner=RecordingRunner(), daemon_id="daemon-a").retry_callbacks()

    assert completed == []
    with db.connect() as conn:
        row = conn.execute("SELECT callback_state FROM cr_tasks WHERE task_id='task1'").fetchone()
    assert row["callback_state"] == "skipped"


def test_daemon_sends_ci_callback_after_feedback_pass_ack(tmp_path: Path) -> None:
    settings = Settings(base_dir=tmp_path / "runtime", review_v2_db_path=tmp_path / "review.sqlite3")
    db = ReviewDB(settings.review_v2_db_path)
    db.init()
    db.create_task(
        task_id="task1",
        app_name="demo",
        repo_url=str(tmp_path),
        branch="feature/a",
        request_json={
            "ci_task_id": "task_ci",
            "ci_record_id": "record_ci",
            "ci_parent_id": "parent_ci",
            "ci_task_template_id": "template_ci",
            "operator": "reviewer",
        },
    )
    db.update_task_outcome("task1", status="success", gate_status="passed")
    callback_client = RecordingCallbackClient()

    completed = CRReviewDaemon(
        settings,
        db=db,
        workflow_runner=RecordingRunner(),
        callback_client=callback_client,
        daemon_id="daemon-a",
    ).retry_callbacks()

    assert completed == ["task1"]
    record, payload = callback_client.calls[0]
    assert record.request.ci_task_id == "task_ci"
    assert record.request.ci_record_id == "record_ci"
    assert payload.passed is True
    with db.connect() as conn:
        row = conn.execute("SELECT callback_state, callback_attempts FROM cr_tasks WHERE task_id='task1'").fetchone()
    assert row["callback_state"] == "succeeded"
    assert row["callback_attempts"] == 1

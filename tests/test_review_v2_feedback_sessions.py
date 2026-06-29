import json
from pathlib import Path

from cr_agent.config import Settings
from cr_agent.review_v2.feedback import FeedbackProcessor
from cr_agent.review_v2.opencode_process import TurnResult
from cr_agent.review_v2.storage import ReviewDB


class FakeFeedbackRunner:
    def __init__(self):
        self.calls = []

    def run_turn(self, **kwargs):
        self.calls.append(kwargs)
        return TurnResult(
            type="completed",
            result='{"resolved":true,"status":"resolved_model_false_positive","reply":"not a real issue"}',
            session_id="ses_feedback",
            tokens={"input": 5, "output": 5, "cache": {"read": 2, "write": 0}, "total": 12},
        )


class PrefixedJsonFeedbackRunner:
    def run_turn(self, **kwargs):
        return TurnResult(
            type="completed",
            result='先核查上下文。\n{"resolved":true,"status":"resolved_model_false_positive","reply":"not a real issue"}',
            session_id="ses_feedback",
            tokens={"total": 1},
        )


class InvalidJsonFeedbackRunner:
    def run_turn(self, **kwargs):
        return TurnResult(type="completed", result="{not-json", session_id="ses_bad")


class RejectedFeedbackRunner:
    def run_turn(self, **kwargs):
        return TurnResult(
            type="completed",
            result='{"resolved":false,"status":null,"reply":"反馈未改变原问题判断，建议保持打开。"}',
            session_id="ses_reject",
            tokens={"total": 3},
        )


def test_feedback_session_resolves_finding_and_recomputes_task_pass(tmp_path: Path) -> None:
    settings = Settings(base_dir=tmp_path / "runtime", review_v2_db_path=tmp_path / "review.sqlite3", review_v2_audit_dir=tmp_path / "audit")
    db = ReviewDB(settings.review_v2_db_path)
    db.init()
    task_id = db.create_task(task_id="task1", app_name="demo", repo_url=str(tmp_path), branch="feature/a")
    run_id = db.create_reviewer_run(
        task_id=task_id,
        reviewer="security",
        workflow_run_id="wf1",
        attempt=1,
        status="success",
        session_id="ses_review",
        token_usage={"total_tokens": 10, "cost_usd": 0.1},
    )
    finding_id = db.create_finding(
        task_id=task_id,
        reviewer_run_id=run_id,
        file_path="src/app.py",
        line=1,
        severity="high",
        title="Bug",
        detail="detail",
    )
    db.update_task_outcome(task_id, status="success", gate_status="failed")

    runner = FakeFeedbackRunner()
    session_id = FeedbackProcessor(settings, db, runner=runner).submit_finding_feedback(
        task_id=task_id,
        finding_id=finding_id,
        message="this is false positive",
    )

    with db.connect() as conn:
        finding = conn.execute("SELECT status FROM findings WHERE finding_id=?", (finding_id,)).fetchone()
        feedback = conn.execute("SELECT * FROM feedback_sessions WHERE feedback_session_id=?", (session_id,)).fetchone()
        task = conn.execute("SELECT gate_status, callback_state FROM cr_tasks WHERE task_id=?", (task_id,)).fetchone()
    assert finding["status"] == "resolved_model_false_positive"
    assert feedback["parent_reviewer_run_id"] == run_id
    assert feedback["opencode_session_id"] == "ses_feedback"
    assert feedback["total_tokens"] == 12
    assert round(feedback["cost_usd"], 6) == 0.000176
    assert task["gate_status"] == "passed"
    assert task["callback_state"] == "skipped"
    prompt_text = runner.calls[0]["prompt_file"].read_text(encoding="utf-8")
    assert "Original contributing reviewer: security" in prompt_text
    assert '"reviewer": "security"' in prompt_text


def test_feedback_start_records_running_sub_session(tmp_path: Path) -> None:
    settings = Settings(base_dir=tmp_path / "runtime", review_v2_db_path=tmp_path / "review.sqlite3", review_v2_audit_dir=tmp_path / "audit")
    db = ReviewDB(settings.review_v2_db_path)
    db.init()
    task_id = db.create_task(task_id="task1", app_name="demo", repo_url=str(tmp_path), branch="feature/a")
    run_id = db.create_reviewer_run(
        task_id=task_id,
        reviewer="api_contract",
        workflow_run_id="wf1",
        attempt=1,
        status="success",
        session_id="ses_review",
    )
    finding_id = db.create_finding(
        task_id=task_id,
        reviewer_run_id=run_id,
        file_path="src/app.py",
        line=1,
        severity="medium",
        title="Bug",
        detail="detail",
    )

    feedback_id, prompt_path = FeedbackProcessor(settings, db, runner=FakeFeedbackRunner()).start_finding_feedback(
        task_id=task_id,
        finding_id=finding_id,
        message="please re-review",
    )

    with db.connect() as conn:
        feedback = conn.execute("SELECT * FROM feedback_sessions WHERE feedback_session_id=?", (feedback_id,)).fetchone()
        event = conn.execute("SELECT * FROM task_events WHERE task_id=? AND event_type='feedback_started'", (task_id,)).fetchone()
    assert feedback["status"] == "running"
    assert feedback["finding_id"] == finding_id
    assert feedback["parent_reviewer_run_id"] == run_id
    assert prompt_path.exists()
    assert event["stage"] == "feedback"


def test_human_non_fix_resolution_does_not_require_opencode(tmp_path: Path) -> None:
    settings = Settings(base_dir=tmp_path / "runtime", review_v2_db_path=tmp_path / "review.sqlite3")
    db = ReviewDB(settings.review_v2_db_path)
    db.init()
    task_id = db.create_task(task_id="task1", app_name="demo", repo_url=str(tmp_path), branch="feature/a")
    finding_id = db.create_finding(
        task_id=task_id,
        reviewer_run_id=None,
        file_path="src/app.py",
        line=1,
        severity="medium",
        title="Accepted risk",
        detail="detail",
    )
    db.update_task_outcome(task_id, status="success", gate_status="failed")

    FeedbackProcessor(settings, db).mark_human_non_fix(
        task_id=task_id,
        finding_id=finding_id,
        actor="reviewer",
        rationale="accepted for release",
    )

    with db.connect() as conn:
        finding = conn.execute("SELECT status FROM findings WHERE finding_id=?", (finding_id,)).fetchone()
        task = conn.execute("SELECT gate_status, callback_state FROM cr_tasks WHERE task_id=?", (task_id,)).fetchone()
    assert finding["status"] == "human_non_fix"
    assert task["gate_status"] == "passed"
    assert task["callback_state"] == "skipped"


def test_human_non_fix_resolution_queues_ci_callback_when_target_exists(tmp_path: Path) -> None:
    settings = Settings(base_dir=tmp_path / "runtime", review_v2_db_path=tmp_path / "review.sqlite3")
    db = ReviewDB(settings.review_v2_db_path)
    db.init()
    task_id = db.create_task(
        task_id="task1",
        app_name="demo",
        repo_url=str(tmp_path),
        branch="feature/a",
        request_json={
            "ci_task_id": "task_ci",
            "ci_record_id": "record_ci",
            "ci_parent_id": "parent_ci",
            "ci_task_template_id": "template_ci",
        },
    )
    finding_id = db.create_finding(
        task_id=task_id,
        reviewer_run_id=None,
        file_path="src/app.py",
        line=1,
        severity="medium",
        title="Accepted risk",
        detail="detail",
    )
    db.update_task_outcome(task_id, status="success", gate_status="failed")

    FeedbackProcessor(settings, db).mark_human_non_fix(
        task_id=task_id,
        finding_id=finding_id,
        actor="reviewer",
        rationale="accepted for release",
    )

    with db.connect() as conn:
        task = conn.execute("SELECT gate_status, callback_state, callback_next_retry_at FROM cr_tasks WHERE task_id=?", (task_id,)).fetchone()
    assert task["gate_status"] == "passed"
    assert task["callback_state"] == "retrying"
    assert task["callback_next_retry_at"]


def test_feedback_accepts_json_after_progress_text(tmp_path: Path) -> None:
    settings = Settings(base_dir=tmp_path / "runtime", review_v2_db_path=tmp_path / "review.sqlite3", review_v2_audit_dir=tmp_path / "audit")
    db = ReviewDB(settings.review_v2_db_path)
    db.init()
    task_id = db.create_task(task_id="task1", app_name="demo", repo_url=str(tmp_path), branch="feature/a")
    finding_id = db.create_finding(
        task_id=task_id,
        reviewer_run_id=None,
        file_path="src/app.py",
        line=1,
        severity="high",
        title="Bug",
        detail="detail",
    )

    FeedbackProcessor(settings, db, runner=PrefixedJsonFeedbackRunner()).submit_finding_feedback(
        task_id=task_id,
        finding_id=finding_id,
        message="this is false positive",
    )

    with db.connect() as conn:
        finding = conn.execute("SELECT status FROM findings WHERE finding_id=?", (finding_id,)).fetchone()
    assert finding["status"] == "resolved_model_false_positive"


def test_feedback_ignores_progress_json_without_feedback_schema(tmp_path: Path) -> None:
    settings = Settings(base_dir=tmp_path / "runtime", review_v2_db_path=tmp_path / "review.sqlite3", review_v2_audit_dir=tmp_path / "audit")
    db = ReviewDB(settings.review_v2_db_path)
    db.init()
    task_id = db.create_task(task_id="task1", app_name="demo", repo_url=str(tmp_path), branch="feature/a")
    finding_id = db.create_finding(
        task_id=task_id,
        reviewer_run_id=None,
        file_path="src/app.py",
        line=1,
        severity="high",
        title="Bug",
        detail="detail",
    )

    class Runner:
        def run_turn(self, **kwargs):
            return TurnResult(
                type="completed",
                result='先举例 {"status":"open"}。\n{"resolved":true,"status":"resolved_model_false_positive","reply":"ok"}',
                session_id="ses_feedback",
                tokens={"total": 1},
            )

    FeedbackProcessor(settings, db, runner=Runner()).submit_finding_feedback(
        task_id=task_id,
        finding_id=finding_id,
        message="this is false positive",
    )

    with db.connect() as conn:
        finding = conn.execute("SELECT status FROM findings WHERE finding_id=?", (finding_id,)).fetchone()
    assert finding["status"] == "resolved_model_false_positive"


def test_feedback_rejects_missing_finding_before_opencode(tmp_path: Path) -> None:
    settings = Settings(base_dir=tmp_path / "runtime", review_v2_db_path=tmp_path / "review.sqlite3", review_v2_audit_dir=tmp_path / "audit")
    db = ReviewDB(settings.review_v2_db_path)
    db.init()
    task_id = db.create_task(task_id="task1", app_name="demo", repo_url=str(tmp_path), branch="feature/a")
    runner = FakeFeedbackRunner()

    try:
        FeedbackProcessor(settings, db, runner=runner).submit_finding_feedback(
            task_id=task_id,
            finding_id="missing",
            message="not related",
        )
    except ValueError as exc:
        assert "finding not found" in str(exc)
    else:
        raise AssertionError("missing finding should fail")


def test_feedback_invalid_json_records_failed_session(tmp_path: Path) -> None:
    settings = Settings(base_dir=tmp_path / "runtime", review_v2_db_path=tmp_path / "review.sqlite3", review_v2_audit_dir=tmp_path / "audit")
    db = ReviewDB(settings.review_v2_db_path)
    db.init()
    task_id = db.create_task(task_id="task1", app_name="demo", repo_url=str(tmp_path), branch="feature/a")
    finding_id = db.create_finding(
        task_id=task_id,
        reviewer_run_id=None,
        file_path="src/app.py",
        line=1,
        severity="high",
        title="Bug",
        detail="detail",
    )

    feedback_id = FeedbackProcessor(settings, db, runner=InvalidJsonFeedbackRunner()).submit_finding_feedback(
        task_id=task_id,
        finding_id=finding_id,
        message="this is false positive",
    )

    with db.connect() as conn:
        feedback = conn.execute("SELECT status FROM feedback_sessions WHERE feedback_session_id=?", (feedback_id,)).fetchone()
        finding = conn.execute("SELECT status FROM findings WHERE finding_id=?", (finding_id,)).fetchone()
    assert feedback["status"] == "failed"
    assert finding["status"] == "open"


def test_feedback_rejection_persists_model_reply(tmp_path: Path) -> None:
    settings = Settings(base_dir=tmp_path / "runtime", review_v2_db_path=tmp_path / "review.sqlite3", review_v2_audit_dir=tmp_path / "audit")
    db = ReviewDB(settings.review_v2_db_path)
    db.init()
    task_id = db.create_task(task_id="task1", app_name="demo", repo_url=str(tmp_path), branch="feature/a")
    finding_id = db.create_finding(
        task_id=task_id,
        reviewer_run_id=None,
        file_path="src/app.py",
        line=1,
        severity="high",
        title="Bug",
        detail="detail",
    )

    feedback_id = FeedbackProcessor(settings, db, runner=RejectedFeedbackRunner()).submit_finding_feedback(
        task_id=task_id,
        finding_id=finding_id,
        message="this is false positive",
    )

    with db.connect() as conn:
        feedback = conn.execute("SELECT * FROM feedback_sessions WHERE feedback_session_id=?", (feedback_id,)).fetchone()
        finding = conn.execute("SELECT status FROM findings WHERE finding_id=?", (finding_id,)).fetchone()
        event = conn.execute(
            "SELECT payload_json FROM task_events WHERE task_id=? AND event_type='feedback_failed' ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
    assert feedback["status"] == "failed"
    assert feedback["opencode_session_id"] == "ses_reject"
    assert feedback["model_reply"] == "反馈未改变原问题判断，建议保持打开。"
    assert finding["status"] == "open"
    assert json.loads(event["payload_json"])["reply"] == "反馈未改变原问题判断，建议保持打开。"

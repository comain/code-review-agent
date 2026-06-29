from pathlib import Path

from cr_agent.config import Settings
from cr_agent.review_v2.storage import ReviewDB
from cr_agent.review_v2.views import ReviewV2Views


def test_report_detail_includes_feedback_sessions_and_combined_usage(tmp_path: Path) -> None:
    settings = Settings(base_dir=tmp_path / "runtime", review_v2_db_path=tmp_path / "review.sqlite3")
    db = ReviewDB(settings.review_v2_db_path)
    db.init()
    task_id = db.create_task(
        task_id="task1",
        app_name="demo",
        repo_url=str(tmp_path),
        branch="feature/a",
        commit_id="abc123",
        request_json={"source": "manual"},
    )
    run_id = db.create_reviewer_run(
        task_id=task_id,
        reviewer="correctness",
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
        line=7,
        severity="medium",
        title="Bug",
        detail="detail",
    )
    db.create_feedback_session(
        task_id=task_id,
        finding_id=finding_id,
        parent_reviewer_run_id=run_id,
        status="success",
        opencode_session_id="ses_feedback",
        feedback_text="db尚未更新, 审批中",
        model_reply="反馈确认数据库尚未更新，问题保持打开。",
        token_usage={"total_tokens": 5, "cost_usd": 0.05},
    )

    detail = ReviewV2Views(db).report_detail(task_id)
    progress = ReviewV2Views(db).feedback_progress(task_id, detail["feedback_sessions"][0]["feedback_session_id"])

    assert detail["token_usage"]["total_tokens"] == 15
    assert detail["token_usage"]["cost_usd"] == 0.15000000000000002
    assert detail["findings"][0]["source_reviewer"] == "correctness"
    assert detail["findings"][0]["source_reviewer_run_id"] == run_id
    assert detail["findings"][0]["source_session_id"] == "ses_review"
    assert detail["feedback_sessions"][0]["parent_reviewer_run_id"] == run_id
    assert detail["feedback_sessions"][0]["parent_reviewer"] == "correctness"
    assert detail["feedback_sessions"][0]["opencode_session_id"] == "ses_feedback"
    assert detail["feedback_sessions"][0]["feedback_text"] == "db尚未更新, 审批中"
    assert detail["feedback_sessions"][0]["model_reply"] == "反馈确认数据库尚未更新，问题保持打开。"
    assert progress["model_reply"] == "反馈确认数据库尚未更新，问题保持打开。"
    assert detail["commit_id"] == "abc123"
    assert detail["trigger_source"] == "manual"
    assert detail["review_session_count"] == 1
    assert detail["feedback_session_count"] == 1


def test_skipped_report_detail_counts_as_passed(tmp_path: Path) -> None:
    settings = Settings(base_dir=tmp_path / "runtime", review_v2_db_path=tmp_path / "review.sqlite3")
    db = ReviewDB(settings.review_v2_db_path)
    db.init()
    skipped_id = db.create_task(task_id="skipped", app_name="demo", repo_url=str(tmp_path), branch="docs")
    failed_id = db.create_task(task_id="failed", app_name="demo", repo_url=str(tmp_path), branch="code")
    db.update_task_outcome(skipped_id, status="success", gate_status="skipped")
    db.update_task_outcome(failed_id, status="failed", gate_status="incomplete")

    views = ReviewV2Views(db)
    detail = views.report_detail(skipped_id)
    recent = views.recent_report(hours=24, limit=10)

    assert detail["pass_check"] is True
    assert recent["summary"]["passed"] == 1
    assert recent["summary"]["failed"] == 1

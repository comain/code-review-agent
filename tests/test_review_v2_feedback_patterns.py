from pathlib import Path

from cr_agent.config import Settings
from cr_agent.review_v2 import feedback_patterns
from cr_agent.review_v2.feedback_patterns import TARGET_PATTERNS_REL, TARGET_REVIEW_REFERENCE_REL
from cr_agent.review_v2.storage import ReviewDB


def test_sync_feedback_patterns_creates_mr_from_isolated_worktree(tmp_path: Path, monkeypatch) -> None:
    local_override_path = tmp_path / "false_positive_patterns.md"
    work_repo = tmp_path / "work_repo"
    (work_repo / TARGET_PATTERNS_REL.parent).mkdir(parents=True)
    (work_repo / TARGET_PATTERNS_REL).write_text("# old patterns\n", encoding="utf-8")
    (work_repo / TARGET_REVIEW_REFERENCE_REL.parent).mkdir(parents=True, exist_ok=True)
    (work_repo / TARGET_REVIEW_REFERENCE_REL).write_text("# review\n", encoding="utf-8")
    settings = Settings(
        base_dir=tmp_path / "runtime",
        review_v2_db_path=tmp_path / "review.sqlite3",
        review_v2_feedback_pattern_output_path=local_override_path,
        review_v2_feedback_pattern_sync_target_branch="init",
    )
    db = ReviewDB(settings.review_v2_db_path)
    db.init()
    task_id = db.create_task(task_id="task1", app_name="demo", repo_url=str(tmp_path), branch="feature/a")
    false_positive_id = db.create_finding(
        task_id=task_id,
        reviewer_run_id=None,
        file_path="src/app.py",
        line=10,
        severity="high",
        title="Debug path flagged as fatal",
        detail="The reviewer treated debug-only code as production behavior.",
        suggestion="Do not block on this debug-only branch.",
    )
    accepted_feedback_id = db.create_finding(
        task_id=task_id,
        reviewer_run_id=None,
        file_path="src/config.py",
        line=20,
        severity="medium",
        title="Accepted rollout risk",
        detail="The report asked for a migration guard.",
        suggestion="Add a guard.",
    )
    db.update_finding_status(
        task_id=task_id,
        finding_id=false_positive_id,
        status="resolved_model_false_positive",
        actor="model",
        message="Debug branch is unreachable in production.",
    )
    db.update_finding_status(
        task_id=task_id,
        finding_id=accepted_feedback_id,
        status="human_non_fix",
        actor="reviewer",
        message="Risk accepted for this rollout.",
    )
    git_calls = []
    monkeypatch.setenv("CR_AGENT_KEEP_SYNC_WORKTREE", "1")
    monkeypatch.setattr(feedback_patterns, "clone_isolated_repo", lambda *args, **kwargs: work_repo)
    monkeypatch.setattr(feedback_patterns, "configure_git_user", lambda *args, **kwargs: None)

    def fake_git(repo_root, *args):
        git_calls.append(args)
        return ""

    monkeypatch.setattr(feedback_patterns, "git", fake_git)
    monkeypatch.setattr(
        feedback_patterns,
        "create_merge_request",
        lambda *args, **kwargs: "https://github.com/comain/code-review-agent/pull/123",
    )

    result = feedback_patterns.sync_feedback_patterns_from_db(db, settings)
    repeat = feedback_patterns.sync_feedback_patterns_from_db(db, settings)

    text = (work_repo / TARGET_PATTERNS_REL).read_text(encoding="utf-8")
    assert result.changed is True
    assert result.pushed is True
    assert result.branch is not None
    assert result.branch.startswith("mr/feedback-pattern-sync-")
    assert result.merge_request_url == "https://github.com/comain/code-review-agent/pull/123"
    assert result.manual_merge_request_url is not None
    assert repeat.changed is False
    assert result.false_positive_count == 1
    assert result.accepted_feedback_count == 1
    assert ("push", "-u", "origin", result.branch) in git_calls
    assert not local_override_path.exists()
    assert "Debug path flagged as fatal" in text
    assert "resolved_model_false_positive" in text
    assert "Accepted rollout risk" in text
    assert "human_non_fix" in text
    assert "Risk accepted for this rollout." in text
    assert "false_positive_patterns.md" in (work_repo / TARGET_REVIEW_REFERENCE_REL).read_text(encoding="utf-8")


def test_sync_feedback_patterns_noops_without_resolved_feedback(tmp_path: Path) -> None:
    output_path = tmp_path / "false_positive_patterns.md"
    settings = Settings(
        base_dir=tmp_path / "runtime",
        review_v2_db_path=tmp_path / "review.sqlite3",
        review_v2_feedback_pattern_output_path=output_path,
    )
    db = ReviewDB(settings.review_v2_db_path)
    db.init()
    db.create_task(task_id="task1", app_name="demo", repo_url=str(tmp_path), branch="feature/a")

    result = feedback_patterns.sync_feedback_patterns_from_db(db, settings)

    assert result.changed is False
    assert result.total_count == 0
    assert not output_path.exists()

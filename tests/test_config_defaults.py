from importlib import resources
from pathlib import Path

from cr_agent.config import Settings


def test_review_v2_settings_defaults_are_safe(tmp_path: Path) -> None:
    settings = Settings(base_dir=tmp_path / "runtime")

    assert settings.review_v2_db_path == tmp_path / "runtime" / "review_v2.sqlite3"
    assert settings.review_v2_audit_dir == tmp_path / "runtime" / "review_v2_audit"
    assert settings.review_v2_daemon_poll_interval_seconds == 2.0
    assert settings.review_v2_daemon_claim_limit == 1
    assert settings.review_v2_daemon_lease_seconds == 300
    assert settings.review_v2_daemon_once is False
    assert settings.review_v2_feedback_pattern_sync_enabled is True
    assert settings.review_v2_feedback_pattern_sync_hour == 23
    assert settings.review_v2_feedback_pattern_sync_minute == 38
    assert settings.review_v2_feedback_pattern_sync_limit == 200
    assert settings.review_v2_feedback_pattern_output_path is None
    assert settings.review_v2_feedback_pattern_sync_target_branch == "init"
    assert settings.review_v2_feedback_pattern_sync_work_root is None
    assert settings.review_v2_reviewer_concurrency == 3
    assert settings.review_v2_global_opencode_concurrency == 4
    assert settings.review_v2_context_max_bytes > 0
    assert settings.review_v2_callback_max_attempts == 5
    assert settings.gitlab_api_token == ""
    assert settings.trigger_token == ""


def test_langgraph_dependency_is_importable() -> None:
    import langgraph.graph  # noqa: F401


def test_review_v2_templates_are_packaged() -> None:
    template_root = resources.files("cr_agent.review_v2.templates")

    assert (template_root / "reviewer.md.j2").is_file()
    assert (template_root / "judge.md.j2").is_file()


def test_judge_template_documents_gating_and_rejection_contract() -> None:
    template_root = resources.files("cr_agent.review_v2.templates")
    text = (template_root / "judge.md.j2").read_text(encoding="utf-8")

    assert "Recall pass" in text
    assert "schema_incomplete" in text
    assert "duplicate_merged" in text
    assert "critical` as `fatal" in text
    assert "Do not silently drop a candidate" in text
    assert "accepted_findings" in text

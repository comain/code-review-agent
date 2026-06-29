import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from cr_agent.app import create_app
from cr_agent.config import Settings
from cr_agent.core.service import TaskService
from cr_agent.dependencies import get_task_service


def test_review_v2_modules_do_not_use_opencode_db_fallback() -> None:
    completed = subprocess.run(
        ["rg", "_load_usage_metrics_from_opencode_db|_load_text_parts_from_opencode_db|opencode_db", "src/cr_agent/review_v2"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 1
    assert completed.stdout == ""


def test_review_v2_blocks_legacy_index_feedback_and_fix_session_routes(tmp_path: Path) -> None:
    settings = Settings(
        base_dir=tmp_path / "runtime",
        task_dir=tmp_path / "runtime" / "tasks",
        report_dir=tmp_path / "runtime" / "reports",
        usage_dir=tmp_path / "runtime" / "usage",
        log_dir=tmp_path / "logs",
        issues_dir=tmp_path / "issues",
        review_v2_enabled=True,
    )
    service = TaskService(settings)
    settings.ensure_dirs()
    app = create_app(settings)
    app.dependency_overrides[get_task_service] = lambda: service
    client = TestClient(app)

    index_feedback = client.post("/reports/task1/findings/0/feedback", json={"message": "discuss"})
    fix_session = client.post("/reports/task1/fix-sessions", json={"selected_finding_indexes": [0]})

    assert index_feedback.status_code == 404
    assert fix_session.status_code == 404

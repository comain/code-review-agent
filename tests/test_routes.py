from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from cr_agent.app import create_app
from cr_agent.config import Settings
from cr_agent.core.service import TaskService
from cr_agent.dependencies import get_task_service
from cr_agent.models import AnalysisResult, TaskRecord, TaskStatus, TriggerRequest
from cr_agent.review_v2.storage import ReviewDB


def build_settings(tmp_path: Path) -> Settings:
    settings = Settings(
        base_dir=tmp_path / "runtime",
        repo_cache_dir=tmp_path / "runtime" / "repos",
        task_dir=tmp_path / "runtime" / "tasks",
        report_dir=tmp_path / "runtime" / "reports",
        usage_dir=tmp_path / "runtime" / "usage",
        log_dir=tmp_path / "logs",
        issues_dir=tmp_path / "issues",
    )
    settings.ensure_dirs()
    return settings


class FakeTriggerService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.requests: list[TriggerRequest] = []

    def submit(self, request: TriggerRequest) -> TaskRecord:
        self.requests.append(request)
        return TaskRecord(task_id="task1", status=TaskStatus.queued, request=request)


def _trigger_payload() -> dict[str, str]:
    return {
        "app_name": "demo",
        "repo_url": "git@github.com:comain/code-review-agent.git",
        "branch": "feature/auth",
    }


def test_trigger_routes_require_configured_token(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    service = FakeTriggerService(settings)
    app = create_app(settings)
    app.dependency_overrides[get_task_service] = lambda: service
    client = TestClient(app)

    response = client.post("/api/v1/tasks/trigger", json=_trigger_payload(), headers={"Authorization": "Bearer any"})

    assert response.status_code == 503
    assert service.requests == []


def test_trigger_routes_reject_missing_or_invalid_token(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    settings.trigger_token = "secret"
    service = FakeTriggerService(settings)
    app = create_app(settings)
    app.dependency_overrides[get_task_service] = lambda: service
    client = TestClient(app)

    missing = client.post("/api/v1/tasks/trigger", json=_trigger_payload())
    invalid = client.post("/api/v1/hooks/trigger", json=_trigger_payload(), headers={"X-CR-Agent-Token": "wrong"})

    assert missing.status_code == 401
    assert invalid.status_code == 401
    assert service.requests == []


def test_trigger_routes_accept_bearer_token(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    settings.trigger_token = "secret"
    service = FakeTriggerService(settings)
    app = create_app(settings)
    app.dependency_overrides[get_task_service] = lambda: service
    client = TestClient(app)

    response = client.post(
        "/api/v1/tasks/trigger",
        json=_trigger_payload(),
        headers={"Authorization": "Bearer secret"},
    )

    assert response.status_code == 200
    assert response.json()["task_id"] == "task1"
    assert service.requests[0].branch == "feature/auth"


def test_ci_trigger_accepts_shared_token_header(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    settings.trigger_token = "secret"
    service = FakeTriggerService(settings)
    app = create_app(settings)
    app.dependency_overrides[get_task_service] = lambda: service
    client = TestClient(app)

    response = client.post(
        "/api/v1/ci/trigger",
        json={"attribute": {"appName": "demo", "gitUrl": "git@github.com:comain/code-review-agent.git", "branch": "feature/auth"}},
        headers={"X-Webhook-Token": "secret"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == 0
    assert service.requests[0].trigger_source == "ci"


def test_recent_reports_routes_return_data_and_html(monkeypatch, tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    service = TaskService(settings)
    request = TriggerRequest.model_validate(
        {
            "app_name": "demo",
            "repo_url": "git@github.com:comain/code-review-agent.git",
            "branch": "feature/recent",
        }
    )
    service.store.save(
        TaskRecord(
            task_id="task1",
            status=TaskStatus.success,
            request=request,
            created_at=datetime.now(timezone.utc),
            opencode_session_ids=["ses_task"],
            result=AnalysisResult(summary="done", pass_check=True, score=95, findings=[]),
            report_url="http://example.com/reports/task1/index.html",
        )
    )
    monkeypatch.setattr(
        "cr_agent.core.service.OpencodeRunner._load_usage_metrics_from_opencode_db",
        lambda session_ids: {
            "calls": 1,
            "prompt_tokens": 1000,
            "completion_tokens": 100,
            "thinking_tokens": 50,
            "cache_read_tokens": 200,
            "total_tokens": 1350,
        },
    )

    app = create_app(settings)
    app.dependency_overrides[get_task_service] = lambda: service
    client = TestClient(app)

    data_response = client.get("/reports/recent/data?hours=24&limit=20")
    assert data_response.status_code == 200
    assert data_response.json()["tasks"][0]["task_id"] == "task1"
    token_usage = data_response.json()["tasks"][0]["token_usage"]
    assert token_usage["cache_read_tokens"] == 200
    assert token_usage["total_tokens"] == 1350
    assert token_usage["cost_usd"] == 0.003

    html_response = client.get("/reports/recent.html?hours=24&limit=20")
    assert html_response.status_code == 200
    assert "CR 最近 LLM 任务" in html_response.text
    assert "feature/recent" in html_response.text
    assert "$0.0030 · 1.4K tokens" in html_response.text


def test_review_v2_routes_read_recent_status_and_detail_from_sqlite(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    settings.review_v2_enabled = True
    db = ReviewDB(settings.review_v2_db_path)
    db.init()
    task_id = db.create_task(
        task_id="task1",
        app_name="demo",
        repo_url="git@github.com:comain/code-review-agent.git",
        branch="feature/v2",
        commit_id="abc123def456",
        request_json={"ci_task_id": "ci-task", "ci_record_id": "ci-record"},
    )
    db.create_reviewer_run(
        task_id=task_id,
        reviewer="correctness_light",
        workflow_run_id="wf1",
        attempt=1,
        status="success",
        session_id="ses1",
        model_id="llm-proxy/gpt-5.5",
        token_usage={"input_tokens": 100, "cache_read_tokens": 20, "output_tokens": 30, "total_tokens": 150, "cost_usd": 0.1234},
    )
    db.add_task_event(task_id, "reviewer_started", "correctness_light started", stage="reviewer:correctness_light")
    db.add_task_event(task_id, "reviewer_completed", "correctness_light success", stage="reviewer:correctness_light")
    db.add_task_event(task_id, "judge_completed", "cr_judge completed", stage="judge")
    run_id = db.current_successful_reviewer_runs(task_id)[0]["id"]
    high_finding_id = db.create_finding(
        task_id=task_id,
        reviewer_run_id=run_id,
        file_path="src/app.py",
        line=1,
        severity="high",
        title="Bug",
        detail="detail",
    )
    db.create_finding(
        task_id=task_id,
        reviewer_run_id=run_id,
        file_path="src/style.py",
        line=2,
        severity="low",
        title="Cleanup",
        detail="minor",
    )
    feedback_session_id = db.create_feedback_session(
        task_id=task_id,
        finding_id=high_finding_id,
        parent_reviewer_run_id=run_id,
        status="running",
        feedback_text="db尚未更新, 审批中",
    )
    db.update_task_outcome(task_id, status="success", gate_status="failed", report_url="http://example/reports/task1/index.html")
    service = TaskService(settings)
    app = create_app(settings)
    app.dependency_overrides[get_task_service] = lambda: service
    client = TestClient(app)

    recent = client.get("/reports/recent/data?hours=24&limit=20")
    recent_html = client.get("/reports/recent.html?hours=24&limit=20")
    page = client.get("/task-status/task1")
    status = client.get("/task-status/task1/data")
    detail = client.get("/reports/task1/detail")
    report_page = client.get("/reports/task1/index.html")
    progress = client.get("/reports/task1/progress/data")
    progress_page = client.get("/reports/task1/progress")
    feedback_progress = client.get(f"/reports/task1/feedback-sessions/{feedback_session_id}/progress/data")
    feedback_progress_page = client.get(f"/reports/task1/feedback-sessions/{feedback_session_id}/progress")

    assert recent.status_code == 200
    assert recent.json()["tasks"][0]["token_usage"]["total_tokens"] == 150
    assert recent.json()["tasks"][0]["findings_count"] == 2
    assert recent.json()["summary"]["failed"] == 1
    assert recent_html.status_code == 200
    assert "CR 最近 LLM 任务" in recent_html.text
    assert "feature/v2" in recent_html.text
    assert page.status_code == 200
    assert "代码扫描任务状态" in page.text
    assert "/reports/task1/progress" in page.text
    assert "了解 CR v2 审查逻辑" in page.text
    assert "README.md#cr-v2-overview" in page.text
    assert "问题列表" not in page.text
    assert "Bug" not in page.text
    assert "src/app.py:1" not in page.text
    assert status.status_code == 200
    assert status.json()["gate_status"] == "failed"
    assert status.json()["review_sessions"][0]["session_id"] == "ses1"
    assert status.json()["review_sessions"][0]["model_id"] == "llm-proxy/gpt-5.5"
    assert "reviewer_started" in [item["event_type"] for item in status.json()["events"]]
    assert detail.status_code == 200
    assert detail.json()["findings"][0]["finding_id"].startswith("fnd_")
    assert detail.json()["score"] == 80
    assert detail.json()["commit_id"] == "abc123def456"
    assert detail.json()["trigger_source"] == "ci"
    assert detail.json()["review_session_count"] == 1
    assert detail.json()["findings_count"] == 2
    finding_id = detail.json()["findings"][0]["finding_id"]
    assert report_page.status_code == 200
    assert "demo 大模型扫描报告" in report_page.text
    assert "报告信息" in report_page.text
    assert "任务ID" in report_page.text
    assert "feature/v2" in report_page.text
    assert "abc123def456" in report_page.text
    assert "触发来源" in report_page.text
    assert "ci" in report_page.text
    assert "摘要" in report_page.text
    assert "Token" in report_page.text or "tokens" in report_page.text
    assert "问题列表 (2)" in report_page.text
    assert "Blocking · high (1)" in report_page.text
    assert "Non-blocking · low (1)" in report_page.text
    assert "reviewer: correctness_light" in report_page.text
    assert "Bug" in report_page.text
    assert "src/app.py:1" in report_page.text
    assert "Cleanup" in report_page.text
    assert "src/style.py:2" in report_page.text
    assert "审查会话" in report_page.text
    assert "correctness_light" in report_page.text
    assert "ses1" in report_page.text
    assert "其他反馈（漏判）" in report_page.text
    assert "running" in report_page.text
    assert "db尚未更新, 审批中" in report_page.text
    assert "const hasRunningFeedback = true" in report_page.text
    assert f"/reports/task1/feedback-sessions/{feedback_session_id}/progress" in report_page.text
    assert "一键修复" in report_page.text
    assert "只读审查" in report_page.text
    assert f"feedback-input-{finding_id}" in report_page.text
    assert "反馈复审" in report_page.text
    assert "人工标记非修复" in report_page.text
    assert "/findings/by-id/" in report_page.text
    assert progress.status_code == 200
    assert progress.json()["current_stage"] == "failed"
    assert progress.json()["review_sessions"][0]["total_tokens"] == 150
    assert ("cr_judge", "success") in [(item["reviewer"], item["status"]) for item in progress.json()["review_sessions"]]
    assert f"/reports/${{encodeURIComponent(taskId)}}/feedback-sessions/${{encodeURIComponent(item.feedback_session_id || '')}}/progress" in progress_page.text
    assert feedback_progress.status_code == 200
    assert feedback_progress.json()["feedback_session_id"] == feedback_session_id
    assert feedback_progress.json()["status"] == "running"
    assert feedback_progress.json()["parent_reviewer"] == "correctness_light"
    assert feedback_progress.json()["feedback_text"] == "db尚未更新, 审批中"
    assert feedback_progress.json()["finding"]["finding_id"] == high_finding_id
    assert feedback_progress_page.status_code == 200
    assert "Feedback 复审进度" in feedback_progress_page.text
    assert "db尚未更新, 审批中" in feedback_progress_page.text
    assert feedback_session_id in feedback_progress_page.text
    assert "reviewer_completed" in [item["event_type"] for item in progress.json()["events"]]
    assert progress_page.status_code == 200
    assert "Model" in progress_page.text
    assert "Reviewer Sessions" in progress_page.text
    assert "Feedback Sessions" in progress_page.text
    assert "Timeline" in progress_page.text


def test_review_v2_progress_shows_full_mode_and_queued_planned_reviewers(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    settings.review_v2_enabled = True
    db = ReviewDB(settings.review_v2_db_path)
    db.init()
    task_id = db.create_task(
        task_id="task1",
        app_name="demo",
        repo_url="git@github.com:comain/code-review-agent.git",
        branch="feature/v2",
        status="running",
    )
    db.create_reviewer_plan(
        task_id=task_id,
        workflow_run_id="wf1",
        reviewer="correctness",
        required=True,
        risk_tier="full",
        reason="baseline",
    )
    db.create_reviewer_plan(
        task_id=task_id,
        workflow_run_id="wf1",
        reviewer="security",
        required=True,
        risk_tier="full",
        reason="auth-sensitive path",
    )
    db.start_reviewer_run(task_id=task_id, reviewer="correctness", workflow_run_id="wf1", attempt=1)
    service = TaskService(settings)
    app = create_app(settings)
    app.dependency_overrides[get_task_service] = lambda: service
    client = TestClient(app)

    progress = client.get("/reports/task1/progress/data")
    progress_page = client.get("/reports/task1/progress")

    assert progress.status_code == 200
    payload = progress.json()
    assert payload["review_mode"] == "full"
    assert payload["current_stage"] == "reviewer_fanout:full"
    assert payload["reviewer_plan"][0]["reviewer"] == "correctness"
    assert [(item["reviewer"], item["status"]) for item in payload["review_sessions"]] == [
        ("correctness", "running"),
        ("security", "queued"),
    ]
    assert payload["review_sessions"][1]["reason"] == "auth-sensitive path"
    assert progress_page.status_code == 200
    assert "审查模式" in progress_page.text
    assert "Required" in progress_page.text
    assert "Error / Reason" in progress_page.text


def test_review_v2_admin_task_control_routes(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    settings.review_v2_enabled = True
    db = ReviewDB(settings.review_v2_db_path)
    db.init()
    task_id = db.create_task(task_id="task1", app_name="demo", repo_url="git@github.com:comain/code-review-agent.git", branch="feature/v2")
    db.claim_next_task(daemon_id="daemon-a", lease_seconds=60)
    service = TaskService(settings)
    app = create_app(settings)
    app.dependency_overrides[get_task_service] = lambda: service
    client = TestClient(app)

    stop = client.post("/api/v1/admin/tasks/task1/stop?reason=pause")
    assert stop.status_code == 200
    assert stop.json()["action"] == "stop"
    assert db.check_task_control("task1") == ("stop", "pause")

    cancel = client.post("/api/v1/admin/tasks/task1/cancel?reason=abort")
    assert cancel.status_code == 200
    assert cancel.json()["status"]["status"] == "cancelled"

    requeue = client.post("/api/v1/admin/tasks/task1/requeue?reason=retry")
    assert requeue.status_code == 200
    assert requeue.json()["status"]["status"] == "queued"
    assert requeue.json()["status"]["gate_status"] is None


def test_review_v2_finding_id_feedback_route_marks_human_non_fix(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    settings.review_v2_enabled = True
    db = ReviewDB(settings.review_v2_db_path)
    db.init()
    task_id = db.create_task(task_id="task1", app_name="demo", repo_url="git@github.com:comain/code-review-agent.git", branch="feature/v2")
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
    service = TaskService(settings)
    app = create_app(settings)
    app.dependency_overrides[get_task_service] = lambda: service
    client = TestClient(app)

    response = client.post(
        f"/reports/{task_id}/findings/by-id/{finding_id}/feedback",
        json={"disposition": "human_non_fix", "rationale": "accepted", "actor": "reviewer"},
    )

    assert response.status_code == 200
    with db.connect() as conn:
        task = conn.execute("SELECT gate_status FROM cr_tasks WHERE task_id=?", (task_id,)).fetchone()
        finding = conn.execute("SELECT status FROM findings WHERE finding_id=?", (finding_id,)).fetchone()
    assert finding["status"] == "human_non_fix"
    assert task["gate_status"] == "passed"


def test_review_v2_finding_feedback_route_rejects_missing_finding(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    settings.review_v2_enabled = True
    db = ReviewDB(settings.review_v2_db_path)
    db.init()
    task_id = db.create_task(task_id="task1", app_name="demo", repo_url="git@github.com:comain/code-review-agent.git", branch="feature/v2")
    service = TaskService(settings)
    app = create_app(settings)
    app.dependency_overrides[get_task_service] = lambda: service
    client = TestClient(app)

    response = client.post(
        f"/reports/{task_id}/findings/by-id/missing/feedback",
        json={"disposition": "human_non_fix", "rationale": "accepted"},
    )

    assert response.status_code == 404

from pathlib import Path
import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import pytest

from cr_agent.config import Settings
from cr_agent.core.git_client import GitClient
from cr_agent.core.service import TaskService
from cr_agent.review_v2.storage import ReviewDB
from cr_agent.models import AnalysisResult, CallbackPayload, FindingFeedbackThread, FindingStatus, TaskRecord, TaskStatus, TriggerRequest


def build_settings(tmp_path: Path) -> Settings:
    return Settings(
        base_dir=tmp_path / "runtime",
        repo_cache_dir=tmp_path / "runtime" / "repos",
        task_dir=tmp_path / "runtime" / "tasks",
        report_dir=tmp_path / "runtime" / "reports",
        usage_dir=tmp_path / "runtime" / "usage",
        log_dir=tmp_path / "logs",
        issues_dir=tmp_path / "issues",
    )


def test_submit_uses_review_v2_sqlite_queue_when_enabled(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    settings.review_v2_enabled = True
    service = TaskService(settings)
    request = TriggerRequest.model_validate(
        {
            "app_name": "demo",
            "repo_url": "git@github.com:comain/code-review-agent.git",
            "branch": "feature/v2",
        }
    )

    record = service.submit(request)

    assert record.status == TaskStatus.queued
    assert service.store.get(record.task_id) is None
    with ReviewDB(settings.review_v2_db_path).connect() as conn:
        row = conn.execute("SELECT * FROM cr_tasks WHERE task_id=?", (record.task_id,)).fetchone()
    assert row["status"] == "queued"
    assert row["app_name"] == "demo"
    assert row["branch"] == "feature/v2"


def test_normalize_result_keeps_only_non_test_changed_files() -> None:
    tmp_path = Path("/tmp/cr_agent_test_normalize")
    settings = Settings(issues_dir=tmp_path / "issues")
    service = TaskService(settings)
    result = AnalysisResult.model_validate(
        {
            "summary": "english summary",
            "pass_check": False,
            "score": 72,
            "findings": [
                {
                    "file": "biz/src/main/java/com/foo/ChangedBiz.java",
                    "line": 10,
                    "severity": "high",
                    "title": "issue 1",
                    "detail": "detail 1",
                    "suggestion": "fix 1",
                },
                {
                    "file": "common/src/main/java/com/foo/RetryTemplate.java",
                    "line": 20,
                    "severity": "medium",
                    "title": "issue 2",
                    "detail": "detail 2",
                    "suggestion": "fix 2",
                },
                {
                    "file": "biz/src/test/java/com/foo/ChangedBizTest.java",
                    "line": 30,
                    "severity": "fatal",
                    "title": "issue 3",
                    "detail": "detail 3",
                    "suggestion": "fix 3",
                },
            ],
        }
    )

    normalized = service._normalize_result(
        result,
        {
            "non_test_changed_files": ["biz/src/main/java/com/foo/ChangedBiz.java"],
        },
    )

    assert len(normalized.findings) == 1
    assert normalized.findings[0].file == "biz/src/main/java/com/foo/ChangedBiz.java"
    assert normalized.summary.startswith("本次变更共审查 1 个非测试文件")
    assert normalized.pass_check is False
    assert normalized.score == 85


def test_normalize_result_drops_all_findings_when_changed_files_is_empty() -> None:
    tmp_path = Path("/tmp/cr_agent_test_normalize_empty")
    settings = Settings(issues_dir=tmp_path / "issues")
    service = TaskService(settings)
    result = AnalysisResult.model_validate(
        {
            "summary": "english summary",
            "pass_check": False,
            "score": 72,
            "findings": [
                {
                    "file": "biz/src/main/java/com/foo/ChangedBiz.java",
                    "line": 10,
                    "severity": "high",
                    "title": "issue 1",
                    "detail": "detail 1",
                    "suggestion": "fix 1",
                }
            ],
        }
    )

    normalized = service._normalize_result(
        result,
        {
            "non_test_changed_files": [],
        },
    )

    assert normalized.findings == []
    assert normalized.pass_check is True
    assert normalized.score == 100
    assert normalized.summary == "本次变更共审查 0 个非测试文件，未发现阻断发布的问题。"


def test_submit_returns_existing_inflight_task(tmp_path: Path) -> None:
    service = TaskService(build_settings(tmp_path))
    request = TriggerRequest.model_validate(
        {
            "app_name": "demo",
            "repo_url": "git@github.com:comain/code-review-agent.git",
            "branch": "feature/test",
        }
    )

    first = service.submit(request)
    second = service.submit(request)

    assert first.task_id == second.task_id
    assert len(service.list_all()) == 1


def test_submit_returns_existing_record_for_same_commit(tmp_path: Path) -> None:
    service = TaskService(build_settings(tmp_path))
    request = TriggerRequest.model_validate(
        {
            "app_name": "demo",
            "repo_url": "git@github.com:comain/code-review-agent.git",
            "branch": "feature/test",
            "commit_id": "abc123",
        }
    )
    existing = TaskRecord(task_id="task1", status=TaskStatus.success, request=request)
    service.store.save(existing)

    duplicate = service.submit(request)

    assert duplicate.task_id == "task1"
    assert len(service.list_all()) == 1


def test_submit_deduplicates_under_concurrency(tmp_path: Path) -> None:
    service = TaskService(build_settings(tmp_path))
    request = TriggerRequest.model_validate(
        {
            "app_name": "demo",
            "repo_url": "git@github.com:comain/code-review-agent.git",
            "branch": "feature/test",
        }
    )
    barrier = threading.Barrier(5)
    results = []
    results_lock = threading.Lock()

    def submit_task() -> None:
        barrier.wait()
        record = service.submit(request)
        with results_lock:
            results.append(record.task_id)

    threads = [threading.Thread(target=submit_task) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(set(results)) == 1
    assert len(service.list_all()) == 1


def test_recover_pending_tasks_requeues_queued_and_running(tmp_path: Path) -> None:
    service = TaskService(build_settings(tmp_path))
    request = TriggerRequest.model_validate(
        {
            "app_name": "demo",
            "repo_url": "git@github.com:comain/code-review-agent.git",
            "branch": "feature/test",
        }
    )
    queued = TaskRecord(task_id="queued1", status=TaskStatus.queued, request=request)
    running = TaskRecord(task_id="running1", status=TaskStatus.running, request=request, attempts=1)
    service.store.save(queued)
    service.store.save(running)

    service._recover_pending_tasks()

    recovered_running = service.get("running1")
    assert recovered_running is not None
    assert recovered_running.status == TaskStatus.queued
    assert recovered_running.error_message == "service restart recovery"
    queued_ids = sorted(list(service._queue.queue))
    assert queued_ids == ["queued1", "running1"]


def test_opencode_timeout_requeues_task(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    settings.max_task_attempts = 3
    service = TaskService(settings)
    request = TriggerRequest.model_validate(
        {
            "app_name": "demo",
            "repo_url": "git@github.com:comain/code-review-agent.git",
            "branch": "feature/test",
        }
    )
    record = TaskRecord(task_id="timeout1", status=TaskStatus.queued, request=request)
    service.store.save(record)

    with patch.object(service.git, "prepare_repo", return_value=tmp_path), patch.object(
        service.git, "current_commit", return_value="abc123"
    ), patch.object(service.git, "collect_review_context", return_value={"non_test_changed_files": []}), patch.object(
        service.runner, "analyze", side_effect=RuntimeError("opencode prompt timeout")
    ):
        service._run_task("timeout1")

    updated = service.get("timeout1")
    assert updated is not None
    assert updated.status == TaskStatus.queued
    assert updated.error_message == "opencode prompt timeout"
    assert list(service._queue.queue) == ["timeout1"]


def test_opencode_timeout_stops_retrying_after_max_attempts(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    settings.max_task_attempts = 1
    service = TaskService(settings)
    request = TriggerRequest.model_validate(
        {
            "app_name": "demo",
            "repo_url": "git@github.com:comain/code-review-agent.git",
            "branch": "feature/test",
        }
    )
    record = TaskRecord(task_id="timeout2", status=TaskStatus.queued, request=request)
    service.store.save(record)

    with patch.object(service.git, "prepare_repo", return_value=tmp_path), patch.object(
        service.git, "current_commit", return_value="abc123"
    ), patch.object(service.git, "collect_review_context", return_value={"non_test_changed_files": []}), patch.object(
        service.runner, "analyze", side_effect=RuntimeError("opencode prompt timeout")
    ):
        with patch("cr_agent.core.service.logger.exception"), patch("cr_agent.core.service.logger.warning"), pytest.raises(
            RuntimeError,
            match="opencode prompt timeout",
        ):
            service._run_task("timeout2")

    updated = service.get("timeout2")
    assert updated is not None
    assert updated.status == TaskStatus.failed
    assert updated.error_message == "opencode prompt timeout"


def test_get_dashboard_metrics_groups_counts_and_p99(tmp_path: Path) -> None:
    service = TaskService(build_settings(tmp_path))
    request = TriggerRequest.model_validate(
        {
            "app_name": "demo",
            "repo_url": "git@github.com:comain/code-review-agent.git",
            "branch": "feature/test",
        }
    )
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    record1 = TaskRecord(
        task_id="task1",
        status=TaskStatus.success,
        request=request,
        created_at=now,
        started_at=now,
        finished_at=now + timedelta(seconds=30),
    )
    record2 = TaskRecord(
        task_id="task2",
        status=TaskStatus.failed,
        request=request,
        created_at=now,
        started_at=now,
        finished_at=now + timedelta(seconds=50),
    )
    service.store.save(record1)
    service.store.save(record2)

    metrics = service.get_dashboard_metrics(bucket_minutes=60)

    assert len(metrics["task_counts"]) == 1
    assert metrics["task_counts"][0]["count"] == 2
    assert len(metrics["duration_p99"]) == 1
    assert metrics["duration_p99"][0]["p99_seconds"] >= 30


def test_get_cost_report_aggregates_usage(tmp_path: Path) -> None:
    service = TaskService(build_settings(tmp_path))
    service.usage.append(
        task_id="task1",
        category="analysis",
        prompt_tokens=100,
        completion_tokens=50,
        thinking_tokens=25,
        total_tokens=150,
        returncode=0,
        created_at=datetime(2026, 1, 1, 12, 0, 0),
    )
    service.usage.append(
        task_id="task1",
        category="fix_apply",
        prompt_tokens=200,
        completion_tokens=100,
        thinking_tokens=60,
        total_tokens=300,
        returncode=0,
        created_at=datetime(2026, 1, 1, 12, 30, 0),
    )

    report = service.get_cost_report(
        start_at=datetime(2026, 1, 1, 0, 0, 0),
        end_at=datetime(2026, 1, 1, 23, 59, 59),
        bucket_minutes=60,
    )

    assert report["summary"]["total_calls"] == 2
    assert report["summary"]["total_tokens"] == 450
    assert report["summary"]["total_prompt_tokens"] == 300
    assert report["summary"]["total_completion_tokens"] == 150
    assert report["summary"]["total_thinking_tokens"] == 85
    assert report["summary"]["avg_tokens_per_call"] == 225.0
    categories = {item["category"]: item for item in report["avg_tokens_by_category"]}
    assert categories["analysis"]["avg_tokens"] == 150.0
    assert categories["analysis"]["avg_prompt_tokens"] == 100.0
    assert categories["analysis"]["avg_completion_tokens"] == 50.0
    assert categories["analysis"]["avg_thinking_tokens"] == 25.0
    assert categories["fix_apply"]["avg_tokens"] == 300.0
    assert categories["fix_apply"]["avg_prompt_tokens"] == 200.0
    assert categories["fix_apply"]["avg_completion_tokens"] == 100.0
    assert categories["fix_apply"]["avg_thinking_tokens"] == 60.0
    assert report["tokens_over_time"][0]["prompt_tokens"] == 300
    assert report["tokens_over_time"][0]["completion_tokens"] == 150
    assert report["tokens_over_time"][0]["thinking_tokens"] == 85


def test_delete_running_tasks_removes_matching_records(tmp_path: Path) -> None:
    service = TaskService(build_settings(tmp_path))
    request = TriggerRequest.model_validate(
        {
            "app_name": "demo",
            "repo_url": "git@github.com:comain/code-review-agent.git",
            "branch": "feature/test",
        }
    )
    now = datetime.now(timezone.utc)
    running = TaskRecord(task_id="running1", status=TaskStatus.running, request=request, started_at=now)
    service.store.save(running)

    result = service.delete_running_tasks(start_at=now.replace(second=0), end_at=now.replace(second=59))

    assert result["deleted_count"] == 1
    assert result["task_ids"] == ["running1"]
    assert service.get("running1") is None


def test_list_tasks_page_filters_by_time_and_paginates(tmp_path: Path) -> None:
    service = TaskService(build_settings(tmp_path))
    request = TriggerRequest.model_validate(
        {
            "app_name": "demo",
            "repo_url": "git@github.com:comain/code-review-agent.git",
            "branch": "feature/test",
        }
    )
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    for index in range(3):
        service.store.save(
            TaskRecord(
                task_id=f"task{index}",
                status=TaskStatus.success,
                request=request.model_copy(update={"branch": f"feature/{index}"}),
                created_at=now + timedelta(minutes=index),
            )
        )

    page1 = service.list_tasks_page(
        start_at=now,
        end_at=now + timedelta(minutes=2),
        page=1,
        page_size=2,
    )
    page2 = service.list_tasks_page(
        start_at=now,
        end_at=now + timedelta(minutes=2),
        page=2,
        page_size=2,
    )

    assert page1["total"] == 3
    assert [item["branch"] for item in page1["items"]] == ["feature/2", "feature/1"]
    assert [item["branch"] for item in page2["items"]] == ["feature/0"]
    assert page1["items"][0]["status"] == "success"
    assert page1["items"][0]["created_at"].startswith("2026-01-01T12:02:00")
    assert page1["items"][0]["report_url"] is None


def test_get_recent_task_report_summarizes_recent_records(tmp_path: Path) -> None:
    service = TaskService(build_settings(tmp_path))
    request = TriggerRequest.model_validate(
        {
            "app_name": "demo",
            "repo_url": "git@github.com:comain/code-review-agent.git",
            "branch": "feature/test",
            "ci_task_id": "ci-1",
        }
    )
    now = datetime.now(timezone.utc)
    passed = TaskRecord(
        task_id="passed",
        status=TaskStatus.success,
        request=request,
        created_at=now - timedelta(minutes=10),
        started_at=now - timedelta(minutes=9),
        finished_at=now - timedelta(minutes=8),
        attempts=1,
        report_url="http://example.com/reports/passed/index.html",
        callback_succeeded=True,
        result=AnalysisResult(
            summary="ok",
            pass_check=True,
            score=92,
            findings=[
                {
                    "file": "src/App.java",
                    "line": 10,
                    "severity": "medium",
                    "title": "warn",
                    "detail": "detail",
                    "suggestion": "fix",
                }
            ],
        ),
    )
    failed = TaskRecord(
        task_id="failed",
        status=TaskStatus.failed,
        request=request.model_copy(update={"app_name": "api"}),
        created_at=now - timedelta(minutes=20),
        attempts=2,
        result=AnalysisResult(summary="bad", pass_check=False, score=60, findings=[]),
    )
    old = TaskRecord(
        task_id="old",
        status=TaskStatus.success,
        request=request,
        created_at=now - timedelta(hours=30),
    )
    for record in (passed, failed, old):
        service.store.save(record)

    report = service.get_recent_task_report(hours=24, limit=10)

    assert report["summary"]["total"] == 2
    assert report["summary"]["passed"] == 1
    assert report["summary"]["failed"] == 1
    assert report["summary"]["callback_succeeded"] == 1
    assert report["app_counts"] == {"demo": 1, "api": 1}
    assert report["severity_counts"] == {"medium": 1}
    assert [item["task_id"] for item in report["tasks"]] == ["passed", "failed"]
    assert report["tasks"][0]["duration_seconds"] == 60.0
    assert report["tasks"][0]["gate_status"] == "passed"
    assert report["tasks"][1]["gate_status"] == "failed"


def test_get_recent_task_report_uses_stored_session_ids_for_db_usage(monkeypatch, tmp_path: Path) -> None:
    service = TaskService(build_settings(tmp_path))
    request = TriggerRequest.model_validate(
        {
            "app_name": "demo",
            "repo_url": "git@github.com:comain/code-review-agent.git",
            "branch": "feature/test",
        }
    )
    now = datetime.now(timezone.utc)
    service.store.save(
        TaskRecord(
            task_id="task1",
            status=TaskStatus.success,
            request=request,
            created_at=now,
            opencode_session_ids=["ses_task"],
            result=AnalysisResult(summary="ok", pass_check=True, score=100, findings=[]),
        )
    )
    def fake_db_usage(session_ids: list[str]) -> dict[str, int]:
        assert session_ids == ["ses_task"]
        return {
            "calls": 1,
            "prompt_tokens": 100000,
            "completion_tokens": 5000,
            "thinking_tokens": 500,
            "cache_read_tokens": 50000,
            "total_tokens": 151600,
        }

    monkeypatch.setattr("cr_agent.core.service.OpencodeRunner._load_usage_metrics_from_opencode_db", fake_db_usage)

    report = service.get_recent_task_report(hours=24, limit=10)

    usage = report["tasks"][0]["token_usage"]
    assert usage["source"] == "opencode_db"
    assert usage["calls"] == 1
    assert usage["total_tokens"] == 151600
    assert usage["cache_read_tokens"] == 50000
    assert usage["input_cost_usd"] == 0.1875
    assert usage["output_cost_usd"] == 0.055
    assert usage["cost_usd"] == 0.2425


def test_get_recent_task_report_applies_limit(tmp_path: Path) -> None:
    service = TaskService(build_settings(tmp_path))
    request = TriggerRequest.model_validate(
        {
            "app_name": "demo",
            "repo_url": "git@github.com:comain/code-review-agent.git",
            "branch": "feature/test",
        }
    )
    now = datetime.now(timezone.utc)
    for index in range(3):
        service.store.save(
            TaskRecord(
                task_id=f"task{index}",
                status=TaskStatus.running,
                request=request,
                created_at=now + timedelta(minutes=index),
            )
        )

    report = service.get_recent_task_report(hours=24, limit=2)

    assert report["total_matched"] == 3
    assert report["total_returned"] == 2
    assert [item["task_id"] for item in report["tasks"]] == ["task2", "task1"]
    assert report["summary"]["running_or_queued"] == 2


def test_git_client_returns_same_repo_lock_for_same_repo(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    client = GitClient(settings)

    lock1 = client.repo_lock("git@github.com:comain/code-review-agent.git")
    lock2 = client.repo_lock("git@github.com:comain/code-review-agent.git")
    lock3 = client.repo_lock("git@github.com:comain/other-review-agent.git")

    assert lock1 is lock2
    assert lock1 is not lock3


def test_git_client_refresh_base_refs_updates_remote_tracking_refs(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    client = GitClient(settings)
    repo_path = tmp_path / "repo"
    repo_path.mkdir()

    with patch.object(client, "_run_completed") as mock_run:
        mock_run.return_value = __import__("subprocess").CompletedProcess([], 0, "", "")
        client._refresh_base_refs(repo_path)

    first_cmd = mock_run.call_args_list[0].args[0]
    assert first_cmd[:4] == ["git", "-C", str(repo_path), "fetch"]
    assert first_cmd[4] == "origin"
    assert first_cmd[5:] == ["--prune", "--", "refs/heads/master:refs/remotes/origin/master"]


def test_git_client_refresh_base_refs_skips_missing_remote_ref(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    client = GitClient(settings)
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    (repo_path / ".git_master").write_text("main\n", encoding="utf-8")

    calls = []

    def fake_run_completed(cmd, task_id=None, is_cancelled=None):
        calls.append(cmd)
        if "refs/heads/main:refs/remotes/origin/main" in cmd:
            return __import__("subprocess").CompletedProcess(cmd, 128, "", "fatal: couldn't find remote ref refs/heads/main")
        return __import__("subprocess").CompletedProcess(cmd, 0, "", "")

    with patch.object(client, "_run_completed", side_effect=fake_run_completed):
        client._refresh_base_refs(repo_path)

    assert len(calls) == 1
    assert calls[0][5:] == ["--prune", "--", "refs/heads/main:refs/remotes/origin/main"]


def test_prepare_repo_fetches_branch_into_remote_tracking_ref(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    client = GitClient(settings)
    repo_path = settings.repo_cache_dir / "code-review-agent"
    repo_path.mkdir(parents=True)

    with patch.object(client, "_run") as mock_run:
        client.prepare_repo("git@github.com:comain/code-review-agent.git", "DEMO-3341", None)

    fetch_cmd = mock_run.call_args_list[0].args[0]
    checkout_cmd = mock_run.call_args_list[1].args[0]
    assert fetch_cmd == [
        "git",
        "-C",
        str(repo_path),
        "fetch",
        "origin",
        "--prune",
        "--",
        "refs/heads/DEMO-3341:refs/remotes/origin/DEMO-3341",
    ]
    assert checkout_cmd == ["git", "-C", str(repo_path), "switch", "--detach", "--force", "--", "origin/DEMO-3341"]


def test_prepare_fix_workspace_fetches_branch_into_remote_tracking_ref(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    client = GitClient(settings)
    workspace_dir = tmp_path / "fix-space"

    with patch.object(client, "_run") as mock_run:
        client.prepare_fix_workspace(
            "git@github.com:comain/code-review-agent.git",
            "DEMO-3341",
            None,
            workspace_dir,
            "codex/fix-1",
        )

    fetch_cmd = mock_run.call_args_list[1].args[0]
    checkout_cmd = mock_run.call_args_list[2].args[0]
    assert fetch_cmd == [
        "git",
        "-C",
        str(workspace_dir / "code-review-agent"),
        "fetch",
        "origin",
        "--prune",
        "--",
        "refs/heads/DEMO-3341:refs/remotes/origin/DEMO-3341",
    ]
    assert checkout_cmd == [
        "git",
        "-C",
        str(workspace_dir / "code-review-agent"),
        "switch",
        "--detach",
        "--force",
        "--",
        "origin/DEMO-3341",
    ]


def test_prepare_fix_workspace_clones_after_option_separator(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    client = GitClient(settings)
    workspace_dir = tmp_path / "fix-space"

    with patch.object(client, "_run") as mock_run:
        client.prepare_fix_workspace(
            "git@github.com:comain/code-review-agent.git",
            "DEMO-3341",
            None,
            workspace_dir,
            "codex/fix-1",
        )

    clone_cmd = mock_run.call_args_list[0].args[0]
    assert clone_cmd == [
        "git",
        "clone",
        "--",
        "git@github.com:comain/code-review-agent.git",
        str(workspace_dir / "code-review-agent"),
    ]


def test_git_client_rejects_unsafe_repo_urls_and_refs(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    client = GitClient(settings)

    for repo_url in ("ext::sh -c id", "file:///tmp/repo", "--upload-pack=/bin/sh", "/tmp/repo"):
        with pytest.raises(ValueError):
            client.prepare_repo(repo_url, "feature/test", None)

    for branch in ("--upload-pack=/bin/sh", "feature test", "feature:ref", "feature..main", "feature@{1}"):
        with pytest.raises(ValueError):
            client.prepare_repo("git@github.com:comain/code-review-agent.git", branch, None)


def test_candidate_base_refs_defaults_to_origin_master(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    client = GitClient(settings)
    repo_path = tmp_path / "repo"
    repo_path.mkdir()

    assert client._candidate_base_refs(repo_path) == ["origin/master"]


def test_candidate_base_refs_uses_git_master_when_present(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    client = GitClient(settings)
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    (repo_path / ".git_master").write_text("release/mainline\n", encoding="utf-8")

    assert client._candidate_base_refs(repo_path) == ["origin/release/mainline"]


def test_same_callback_payload_is_not_sent_twice(tmp_path: Path) -> None:
    service = TaskService(build_settings(tmp_path))
    request = TriggerRequest.model_validate(
        {
            "app_name": "demo",
            "repo_url": "git@github.com:comain/code-review-agent.git",
            "branch": "feature/test",
            "callback_url": "http://127.0.0.1:9999/mock/ack",
        }
    )
    record = TaskRecord(task_id="task1", status=TaskStatus.failed, request=request)
    payload = CallbackPayload(
        task_id="task1",
        app_name="demo",
        branch="feature/test",
        passed=False,
        score=80,
        report_url="http://127.0.0.1:8000/reports/task1/index.html",
        status=TaskStatus.failed,
        summary="摘要",
        findings_count=1,
        metadata={},
        generated_at=datetime.now(timezone.utc),
    )

    sent = []

    def fake_send(_record, _payload):
        sent.append("called")
        return [{"attempt": 1, "status_code": 200}]

    service.callback.send = fake_send  # type: ignore[method-assign]

    digest = service._payload_digest(payload)
    record.callback_history = service.callback.send(record, payload)
    record.callback_succeeded = True
    record.callback_payload_digest = digest

    if not (record.callback_succeeded and record.callback_payload_digest == service._payload_digest(payload)):
        record.callback_history = service.callback.send(record, payload)

    assert sent == ["called"]


def test_get_report_detail_uses_effective_feedback_state(tmp_path: Path) -> None:
    service = TaskService(build_settings(tmp_path))
    request = TriggerRequest.model_validate(
        {
            "app_name": "demo",
            "repo_url": "git@github.com:comain/code-review-agent.git",
            "branch": "feature/test",
        }
    )
    record = TaskRecord(
        task_id="task1",
        status=TaskStatus.failed,
        request=request,
        report_url="http://127.0.0.1:8000/reports/task1/index.html",
        report_file=str(tmp_path / "runtime" / "reports" / "task1" / "index.html"),
        result=AnalysisResult.model_validate(
            {
                "summary": "原始摘要",
                "pass_check": False,
                "score": 85,
                "findings": [
                    {
                        "file": "src/main/java/com/foo/Demo.java",
                        "line": 12,
                        "severity": "high",
                        "title": "high issue",
                        "detail": "detail",
                    }
                ],
            }
        ),
    )
    service.store.save(record)
    service.feedback.save_threads(
        "task1",
        {
            0: FindingFeedbackThread(
                finding_index=0,
                status=FindingStatus.resolved_model_false_positive,
            )
        },
    )

    detail = service.get_report_detail("task1")

    assert detail.pass_check is True
    assert detail.score == 100
    assert detail.summary == "本次变更共审查 1 个非测试文件，未发现阻断发布的问题。"
    assert detail.findings[0].status == FindingStatus.resolved_model_false_positive
    assert detail.findings[0].status_label == "已解决-模型误判"


def test_get_report_detail_preserves_reviewed_file_count_for_clean_report(tmp_path: Path) -> None:
    service = TaskService(build_settings(tmp_path))
    request = TriggerRequest.model_validate(
        {
            "app_name": "demo",
            "repo_url": "git@github.com:comain/code-review-agent.git",
            "branch": "feature/test",
        }
    )
    record = TaskRecord(
        task_id="task1",
        status=TaskStatus.success,
        request=request,
        report_url="http://127.0.0.1:8000/reports/task1/index.html",
        result=AnalysisResult.model_validate(
            {
                "summary": "本次变更共审查 23 个非测试文件，未发现阻断发布的问题。",
                "pass_check": True,
                "score": 100,
                "findings": [],
            }
        ),
    )
    service.store.save(record)

    detail = service.get_report_detail("task1")

    assert detail.summary == "本次变更共审查 23 个非测试文件，未发现阻断发布的问题。"
    assert detail.score == 100
    assert detail.pass_check is True


def test_refresh_record_after_feedback_rewrites_report_and_resends_callback(tmp_path: Path) -> None:
    service = TaskService(build_settings(tmp_path))
    request = TriggerRequest.model_validate(
        {
            "app_name": "demo",
            "repo_url": "git@github.com:comain/code-review-agent.git",
            "branch": "feature/test",
            "callback_url": "http://127.0.0.1:9999/mock/ack",
        }
    )
    record = TaskRecord(
        task_id="task1",
        status=TaskStatus.failed,
        request=request,
        report_url="http://127.0.0.1:8000/reports/task1/index.html",
        report_file=str(tmp_path / "runtime" / "reports" / "task1" / "index.html"),
        callback_succeeded=True,
        callback_payload_digest="old",
        result=AnalysisResult.model_validate(
            {
                "summary": "原始摘要",
                "pass_check": False,
                "score": 85,
                "findings": [
                    {
                        "file": "src/main/java/com/foo/Demo.java",
                        "line": 12,
                        "severity": "high",
                        "title": "high issue",
                        "detail": "detail",
                    }
                ],
            }
        ),
    )
    service.store.save(record)
    service.feedback.save_threads(
        "task1",
        {
            0: FindingFeedbackThread(
                finding_index=0,
                status=FindingStatus.resolved_model_false_positive,
            )
        },
    )

    sent_payloads = []

    def fake_send(_record, payload):
        sent_payloads.append(payload)
        return [{"attempt": 1, "status_code": 200}]

    service.callback.send = fake_send  # type: ignore[method-assign]
    service.reporter.write = lambda record, result: (record.report_url, record.report_file)  # type: ignore[method-assign]

    service._refresh_record_after_feedback("task1")

    updated = service.get("task1")
    assert updated is not None
    assert updated.status == TaskStatus.success
    assert updated.result is not None
    assert updated.result.pass_check is True
    assert updated.result.score == 100
    assert len(sent_payloads) == 1
    assert sent_payloads[0].passed is True
    assert sent_payloads[0].status == TaskStatus.success


def test_refresh_record_after_feedback_does_not_callback_when_still_failed(tmp_path: Path) -> None:
    service = TaskService(build_settings(tmp_path))
    request = TriggerRequest.model_validate(
        {
            "app_name": "demo",
            "repo_url": "git@github.com:comain/code-review-agent.git",
            "branch": "feature/test",
            "callback_url": "http://127.0.0.1:9999/mock/ack",
        }
    )
    record = TaskRecord(
        task_id="task1",
        status=TaskStatus.failed,
        request=request,
        report_url="http://127.0.0.1:8000/reports/task1/index.html",
        report_file=str(tmp_path / "runtime" / "reports" / "task1" / "index.html"),
        callback_succeeded=True,
        callback_payload_digest="old",
        result=AnalysisResult.model_validate(
            {
                "summary": "原始摘要",
                "pass_check": False,
                "score": 70,
                "findings": [
                    {
                        "file": "src/main/java/com/foo/Demo.java",
                        "line": 12,
                        "severity": "high",
                        "title": "high issue",
                        "detail": "detail",
                    },
                    {
                        "file": "src/main/java/com/foo/Other.java",
                        "line": 16,
                        "severity": "medium",
                        "title": "medium issue",
                        "detail": "detail",
                    },
                ],
            }
        ),
    )
    service.store.save(record)
    service.feedback.save_threads(
        "task1",
        {
            1: FindingFeedbackThread(
                finding_index=1,
                status=FindingStatus.resolved_model_false_positive,
            )
        },
    )

    sent_payloads = []

    def fake_send(_record, payload):
        sent_payloads.append(payload)
        return [{"attempt": 1, "status_code": 200}]

    service.callback.send = fake_send  # type: ignore[method-assign]
    service.reporter.write = lambda record, result: (record.report_url, record.report_file)  # type: ignore[method-assign]

    service._refresh_record_after_feedback("task1")

    updated = service.get("task1")
    assert updated is not None
    assert updated.status == TaskStatus.failed
    assert updated.result is not None
    assert updated.result.pass_check is False
    assert updated.result.score < 100
    assert sent_payloads == []


def test_submit_general_feedback_saves_processing_item(tmp_path: Path) -> None:
    service = TaskService(build_settings(tmp_path))
    request = TriggerRequest.model_validate(
        {
            "app_name": "demo",
            "repo_url": "git@github.com:comain/code-review-agent.git",
            "branch": "feature/test",
        }
    )
    record = TaskRecord(
        task_id="task1",
        status=TaskStatus.failed,
        request=request,
        result=AnalysisResult.model_validate(
            {
                "summary": "原始摘要",
                "pass_check": False,
                "score": 85,
                "findings": [],
            }
        ),
    )
    service.store.save(record)

    item = service.submit_general_feedback("task1", "这里疑似漏掉了一个 HTTPS 兼容问题")

    saved = service.feedback.load_general_feedbacks("task1")
    assert item.processing is True
    assert len(saved) == 1
    assert saved[0].content == "这里疑似漏掉了一个 HTTPS 兼容问题"
    assert saved[0].processing is True


def test_process_general_feedback_job_writes_missed_issue_file(tmp_path: Path) -> None:
    service = TaskService(build_settings(tmp_path))
    request = TriggerRequest.model_validate(
        {
            "app_name": "demo",
            "repo_url": "git@github.com:comain/code-review-agent.git",
            "branch": "feature/test",
            "commit_id": "abc123",
        }
    )
    record = TaskRecord(
        task_id="task1",
        status=TaskStatus.failed,
        request=request,
        result=AnalysisResult.model_validate(
            {
                "summary": "原始摘要",
                "pass_check": False,
                "score": 85,
                "findings": [],
            }
        ),
    )
    service.store.save(record)
    item = service.submit_general_feedback("task1", "这里疑似漏掉了一个 TLS client 未改造的问题")

    service.git.prepare_repo = lambda **kwargs: tmp_path  # type: ignore[method-assign]
    service.git.repo_lock = lambda _repo_url: __import__("contextlib").nullcontext()  # type: ignore[method-assign]
    service.runner.review_missed_issue_feedback = lambda **kwargs: {  # type: ignore[method-assign]
        "confirmed": True,
        "reply": "确认属于漏判",
        "pattern_summary": "TLS client 漏改造",
    }

    service._process_general_feedback_job("task1", item.feedback_id)

    saved = service.feedback.load_general_feedbacks("task1")
    assert saved[0].processing is False
    assert saved[0].confirmed is True
    issue_files = list((tmp_path / "issues").glob("*.missed-issue.jsonl"))
    assert len(issue_files) == 1
    assert "TLS client 漏改造" in issue_files[0].read_text(encoding="utf-8")


def test_process_feedback_job_writes_accepted_finding_pattern_for_downgrade(tmp_path: Path) -> None:
    service = TaskService(build_settings(tmp_path))
    request = TriggerRequest.model_validate(
        {
            "app_name": "demo",
            "repo_url": "git@github.com:comain/code-review-agent.git",
            "branch": "feature/test",
            "commit_id": "abc123",
        }
    )
    record = TaskRecord(
        task_id="task1",
        status=TaskStatus.failed,
        request=request,
        report_url="http://127.0.0.1:8000/reports/task1/index.html",
        report_file=str(tmp_path / "runtime" / "reports" / "task1" / "index.html"),
        result=AnalysisResult.model_validate(
            {
                "summary": "摘要",
                "pass_check": False,
                "score": 80,
                "findings": [
                    {
                        "file": "src/main/java/com/foo/Demo.java",
                        "line": 12,
                        "severity": "high",
                        "title": "issue",
                        "detail": "detail",
                    }
                ],
            }
        ),
    )
    service.store.save(record)

    service.git.prepare_repo = lambda **kwargs: tmp_path  # type: ignore[method-assign]
    service.git.repo_lock = lambda _repo_url: __import__("contextlib").nullcontext()  # type: ignore[method-assign]
    service.runner.review_finding_feedback = lambda **kwargs: {  # type: ignore[method-assign]
        "action": "downgrade",
        "severity": "low",
        "reply": "接受降级",
        "pattern_summary": "高危判断过严，可降为低风险",
    }
    service._load_code_context = lambda *args, **kwargs: "code"  # type: ignore[method-assign]
    service.reporter.write = lambda record, result: (record.report_url, record.report_file)  # type: ignore[method-assign]
    service.callback.send = lambda _record, _payload: []  # type: ignore[method-assign]

    service.submit_finding_feedback("task1", 0, "这里应该降级")
    service._process_feedback_job("task1", 0)

    issue_files = list((tmp_path / "issues").glob("*.accepted-finding-feedback.jsonl"))
    assert len(issue_files) == 1
    body = issue_files[0].read_text(encoding="utf-8")
    assert '"action": "downgrade"' in body
    assert "高危判断过严，可降为低风险" in body

import json
import subprocess
import threading
import time
from pathlib import Path

from cr_agent.config import Settings
from cr_agent.review_v2.opencode_process import TurnResult
from cr_agent.review_v2.risk import RiskResult
from cr_agent.review_v2.storage import ReviewDB
from cr_agent.review_v2.workflow import WorkflowRunner


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def _repo(tmp_path: Path, file_path: str, content: str) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("demo\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    target = repo / file_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "change")
    return repo


def _repo_with_archive_head_after_code_change(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    repo = tmp_path / "repo"
    _git(tmp_path, "init", "--bare", str(origin))
    _git(tmp_path, "clone", str(origin), str(repo))
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("demo\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    _git(repo, "push", "origin", "HEAD:master")
    _git(repo, "checkout", "-b", "feature/archive")
    app = repo / "src" / "app.py"
    app.parent.mkdir(parents=True, exist_ok=True)
    app.write_text("print('code change')\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "feat: production code")
    docs = repo / "docs" / "archive.md"
    docs.parent.mkdir(parents=True, exist_ok=True)
    docs.write_text("archive docs only\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "docs: archive")
    return repo


class FakeReviewer:
    def __init__(self, result: TurnResult):
        self.result = result
        self.calls = []

    def run_turn(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["prompt_file"].parent.name == "judge":
            return _judge_turn([])
        return self.result


class ReviewerMap:
    def __init__(self, results):
        self.results = results
        self.calls = []

    def run_turn(self, **kwargs):
        reviewer = kwargs["prompt_file"].parent.name
        self.calls.append(reviewer)
        return self.results[reviewer]


class BlockingReviewer:
    def __init__(self, expected_parallel: int):
        self.expected_parallel = expected_parallel
        self.calls = []
        self.active = 0
        self.max_active = 0
        self.condition = threading.Condition()

    def run_turn(self, **kwargs):
        reviewer = kwargs["prompt_file"].parent.name
        if reviewer == "judge":
            return _judge_turn([])
        with self.condition:
            self.calls.append(reviewer)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.condition.notify_all()
            deadline = time.monotonic() + 5
            while self.max_active < self.expected_parallel and time.monotonic() < deadline:
                self.condition.wait(timeout=0.05)
            self.active -= 1
            self.condition.notify_all()
        return TurnResult(
            type="completed",
            result='{"summary":"ok","pass_check":true,"score":100,"findings":[]}',
            session_id=f"ses_{reviewer}",
            tokens={"total": 1},
        )


def _judge_turn(accepted_findings, *, session_id: str = "ses_cr_judge") -> TurnResult:
    return TurnResult(
        type="completed",
        result=json.dumps(
            {
                "accepted_findings": accepted_findings,
                "rejected_candidates": [],
                "accepted_empty_rationale": "no accepted findings" if not accepted_findings else "",
            }
        ),
        session_id=session_id,
        tokens={"total": 1},
    )


class InspectingReviewer:
    def __init__(self, db: ReviewDB, task_id: str, result: TurnResult):
        self.db = db
        self.task_id = task_id
        self.result = result

    def run_turn(self, **kwargs):
        if kwargs["prompt_file"].parent.name == "judge":
            return _judge_turn([])
        with self.db.connect() as conn:
            run = conn.execute("SELECT * FROM reviewer_runs WHERE task_id=?", (self.task_id,)).fetchone()
            event = conn.execute(
                "SELECT * FROM task_events WHERE task_id=? AND event_type='reviewer_started'",
                (self.task_id,),
            ).fetchone()
        assert run["status"] == "running"
        assert run["reviewer"] == "correctness_light"
        assert run["model_id"] == "llm-proxy/gpt-5.5"
        assert run["started_at"]
        assert kwargs["model_id"] == "llm-proxy/gpt-5.5"
        assert event["stage"] == "reviewer:correctness_light"
        return self.result


def _settings(tmp_path: Path) -> Settings:
    settings = Settings(
        base_dir=tmp_path / "runtime",
        report_dir=tmp_path / "reports",
        review_v2_audit_dir=tmp_path / "audit",
        review_v2_db_path=tmp_path / "review.sqlite3",
    )
    settings.ensure_dirs()
    return settings


def test_skipped_workflow_writes_sanitized_report(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "docs/usage.md", "docs only\n")
    settings = _settings(tmp_path)
    db = ReviewDB(settings.review_v2_db_path)
    db.init()
    task_id = db.create_task(task_id="task1", app_name="demo", repo_url=str(repo), branch="feature/docs")

    WorkflowRunner(settings, db, reviewer_runner=FakeReviewer(TurnResult(type="completed"))).run(task_id)

    with db.connect() as conn:
        task = conn.execute("SELECT * FROM cr_tasks WHERE task_id=?", (task_id,)).fetchone()
    report = json.loads((settings.report_dir / task_id / "result.json").read_text(encoding="utf-8"))
    html = (settings.report_dir / task_id / "index.html").read_text(encoding="utf-8")
    assert task["status"] == "success"
    assert task["gate_status"] == "skipped"
    assert report["gate_status"] == "skipped"
    assert "diff --git" not in html
    assert "prompt" not in html.lower()


def test_workflow_prepares_git_url_with_existing_git_client_path(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "docs/usage.md", "docs only\n")
    settings = _settings(tmp_path)
    db = ReviewDB(settings.review_v2_db_path)
    db.init()
    task_id = db.create_task(
        task_id="task1",
        app_name="demo",
        repo_url="git@github.com:comain/code-review-agent.git",
        branch="feature/docs",
        commit_id="abc123",
    )
    calls = []

    def prepare_repo(**kwargs):
        calls.append(kwargs)
        return repo

    WorkflowRunner(
        settings,
        db,
        reviewer_runner=FakeReviewer(TurnResult(type="completed")),
        repo_preparer=prepare_repo,
    ).run(task_id)

    with db.connect() as conn:
        task = conn.execute("SELECT * FROM cr_tasks WHERE task_id=?", (task_id,)).fetchone()
    assert task["status"] == "success"
    assert task["gate_status"] == "skipped"
    assert calls == [
        {
            "repo_url": "git@github.com:comain/code-review-agent.git",
            "branch": "feature/docs",
            "commit_id": "abc123",
            "task_id": task_id,
        }
    ]


def test_light_workflow_runs_required_reviewer_and_passes(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "src/app.py", "print('hi')\n")
    settings = _settings(tmp_path)
    db = ReviewDB(settings.review_v2_db_path)
    db.init()
    task_id = db.create_task(task_id="task1", app_name="demo", repo_url=str(repo), branch="feature/app")
    runner = FakeReviewer(
        TurnResult(
            type="completed",
            result='{"summary":"ok","pass_check":true,"score":100,"findings":[]}',
            session_id="ses_light",
            tokens={"input": 10, "output": 5, "reasoning": 0, "cache": {"read": 2, "write": 1}, "total": 18},
            raw_log_path=str(tmp_path / "raw.jsonl"),
        )
    )

    WorkflowRunner(settings, db, reviewer_runner=runner).run(task_id)

    with db.connect() as conn:
        task = conn.execute("SELECT * FROM cr_tasks WHERE task_id=?", (task_id,)).fetchone()
        run = conn.execute("SELECT * FROM reviewer_runs WHERE task_id=?", (task_id,)).fetchone()
    expected_commit = _git(repo, "rev-parse", "HEAD")
    request_json = json.loads(task["request_json"])
    assert task["status"] == "success"
    assert task["gate_status"] == "passed"
    assert task["commit_id"] == expected_commit
    assert request_json["commit_id"] == expected_commit
    assert run["reviewer"] == "correctness_light"
    assert run["session_id"] == "ses_light"
    assert run["total_tokens"] == 18
    assert round(run["cost_usd"], 6) == 0.000201
    assert runner.calls


def test_workflow_reviews_branch_code_when_head_commit_is_docs_only(tmp_path: Path) -> None:
    repo = _repo_with_archive_head_after_code_change(tmp_path)
    settings = _settings(tmp_path)
    db = ReviewDB(settings.review_v2_db_path)
    db.init()
    task_id = db.create_task(task_id="task1", app_name="demo", repo_url=str(repo), branch="feature/archive")
    runner = FakeReviewer(
        TurnResult(
            type="completed",
            result='{"summary":"ok","pass_check":true,"score":100,"findings":[]}',
            session_id="ses_light",
            tokens={"total": 1},
        )
    )

    WorkflowRunner(settings, db, reviewer_runner=runner).run(task_id)

    with db.connect() as conn:
        task = conn.execute("SELECT * FROM cr_tasks WHERE task_id=?", (task_id,)).fetchone()
        plan = conn.execute("SELECT * FROM reviewer_plans WHERE task_id=?", (task_id,)).fetchone()
    context_dir = settings.review_v2_audit_dir / task_id / "context"
    llm_context = json.loads((context_dir / "llm_context.json").read_text(encoding="utf-8"))
    changed_files = json.loads((context_dir / "changed_files.json").read_text(encoding="utf-8"))

    assert task["status"] == "success"
    assert task["gate_status"] == "passed"
    assert plan["reviewer"] == "correctness_light"
    assert changed_files == ["src/app.py"]
    assert llm_context["diff_range"] != "HEAD~1..HEAD"
    assert len(runner.calls) == 2


def test_workflow_honors_stop_request_before_reviewer_starts(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "src/app.py", "print('hi')\n")
    settings = _settings(tmp_path)
    db = ReviewDB(settings.review_v2_db_path)
    db.init()
    task_id = db.create_task(task_id="task1", app_name="demo", repo_url=str(repo), branch="feature/app")
    db.request_task_stop(task_id, reason="operator stop")
    runner = FakeReviewer(
        TurnResult(
            type="completed",
            result='{"summary":"ok","pass_check":true,"score":100,"findings":[]}',
            session_id="ses_light",
        )
    )

    WorkflowRunner(settings, db, reviewer_runner=runner).run(task_id)

    with db.connect() as conn:
        task = conn.execute("SELECT status, gate_status, error FROM cr_tasks WHERE task_id=?", (task_id,)).fetchone()
        runs = list(conn.execute("SELECT * FROM reviewer_runs WHERE task_id=?", (task_id,)))
    assert runner.calls == []
    assert runs == []
    assert task["status"] == "cancelled"
    assert task["gate_status"] == "stopped"
    assert task["error"] == "operator stop"


def test_workflow_records_reviewer_progress_while_runner_executes(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "src/app.py", "print('hi')\n")
    settings = _settings(tmp_path)
    db = ReviewDB(settings.review_v2_db_path)
    db.init()
    task_id = db.create_task(task_id="task1", app_name="demo", repo_url=str(repo), branch="feature/app")
    runner = InspectingReviewer(
        db,
        task_id,
        TurnResult(
            type="completed",
            result='{"summary":"ok","pass_check":true,"score":100,"findings":[]}',
            session_id="ses_light",
            tokens={"total": 1},
        ),
    )

    WorkflowRunner(settings, db, reviewer_runner=runner).run(task_id)

    with db.connect() as conn:
        run = conn.execute("SELECT * FROM reviewer_runs WHERE task_id=?", (task_id,)).fetchone()
        events = list(conn.execute("SELECT event_type FROM task_events WHERE task_id=? ORDER BY id", (task_id,)))
    assert run["status"] == "success"
    assert run["model_id"] == "llm-proxy/gpt-5.5"
    assert run["finished_at"]
    assert run["duration_seconds"] is not None
    assert "reviewer_started" in [row["event_type"] for row in events]
    assert "reviewer_completed" in [row["event_type"] for row in events]


def test_workflow_accepts_reviewer_json_after_progress_text(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "src/app.py", "print('hi')\n")
    settings = _settings(tmp_path)
    db = ReviewDB(settings.review_v2_db_path)
    db.init()
    task_id = db.create_task(task_id="task1", app_name="demo", repo_url=str(repo), branch="feature/app")
    runner = FakeReviewer(
        TurnResult(
            type="completed",
            result='先做只读核查。\n{"summary":"ok","pass_check":true,"score":100,"findings":[]}',
            session_id="ses_light",
            tokens={"total": 1},
        )
    )

    WorkflowRunner(settings, db, reviewer_runner=runner).run(task_id)

    with db.connect() as conn:
        task = conn.execute("SELECT * FROM cr_tasks WHERE task_id=?", (task_id,)).fetchone()
    assert task["status"] == "success"
    assert task["gate_status"] == "passed"


def test_workflow_ignores_progress_json_without_reviewer_schema(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "src/app.py", "print('hi')\n")
    settings = _settings(tmp_path)
    db = ReviewDB(settings.review_v2_db_path)
    db.init()
    task_id = db.create_task(task_id="task1", app_name="demo", repo_url=str(repo), branch="feature/app")
    runner = FakeReviewer(
        TurnResult(
            type="completed",
            result='先看一个例子 {"file":"src/app.py"}。\n{"summary":"ok","pass_check":true,"score":100,"findings":[]}',
            session_id="ses_light",
            tokens={"total": 1},
        )
    )

    WorkflowRunner(settings, db, reviewer_runner=runner).run(task_id)

    with db.connect() as conn:
        task = conn.execute("SELECT * FROM cr_tasks WHERE task_id=?", (task_id,)).fetchone()
    assert task["status"] == "success"
    assert task["gate_status"] == "passed"


def test_light_workflow_missing_session_fails_incomplete(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "src/app.py", "print('hi')\n")
    settings = _settings(tmp_path)
    db = ReviewDB(settings.review_v2_db_path)
    db.init()
    task_id = db.create_task(task_id="task1", app_name="demo", repo_url=str(repo), branch="feature/app")

    WorkflowRunner(
        settings,
        db,
        reviewer_runner=FakeReviewer(
            TurnResult(type="completed", result='{"summary":"ok","pass_check":true,"score":100,"findings":[]}')
        ),
    ).run(task_id)

    with db.connect() as conn:
        task = conn.execute("SELECT * FROM cr_tasks WHERE task_id=?", (task_id,)).fetchone()
    assert task["status"] == "failed"
    assert task["gate_status"] == "incomplete"


def test_full_workflow_runs_required_specialists_and_persists_findings(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "src/auth/token_service.py", "print('token')\n")
    settings = _settings(tmp_path)
    db = ReviewDB(settings.review_v2_db_path)
    db.init()
    task_id = db.create_task(task_id="task1", app_name="demo", repo_url=str(repo), branch="feature/security")
    reviewer = ReviewerMap(
        {
            "correctness": TurnResult(
                type="completed",
                result='{"summary":"ok","pass_check":true,"score":100,"findings":[]}',
                session_id="ses_correctness",
                tokens={"total": 1},
            ),
            "security": TurnResult(
                type="completed",
                result=(
                    '{"summary":"security issue","pass_check":false,"score":60,'
                    '"findings":[{"file":"src/auth/token_service.py","line":1,"severity":"critical",'
                    '"title":"Token leak","detail":"token is printed","confidence":0.9}]}'
                ),
                session_id="ses_security",
                tokens={"total": 2},
            ),
            "api_contract": TurnResult(
                type="completed",
                result='{"summary":"ok","pass_check":true,"score":100,"findings":[]}',
                session_id="ses_api_contract",
                tokens={"total": 1},
            ),
            "config_release": TurnResult(
                type="completed",
                result='{"summary":"ok","pass_check":true,"score":100,"findings":[]}',
                session_id="ses_config_release",
                tokens={"total": 1},
            ),
            "performance": TurnResult(
                type="completed",
                result='{"summary":"ok","pass_check":true,"score":100,"findings":[]}',
                session_id="ses_performance",
                tokens={"total": 1},
            ),
            "judge": _judge_turn(
                [
                    {
                        "file_path": "src/auth/token_service.py",
                        "line": 1,
                        "severity": "fatal",
                        "title": "Token leak",
                        "detail": "token is printed",
                        "suggestion": "remove the print",
                        "confidence": 0.9,
                        "source_reviewer": "security",
                    }
                ]
            ),
        }
    )

    WorkflowRunner(settings, db, reviewer_runner=reviewer).run(task_id)

    with db.connect() as conn:
        task = conn.execute("SELECT * FROM cr_tasks WHERE task_id=?", (task_id,)).fetchone()
        runs = list(conn.execute("SELECT reviewer, session_id FROM reviewer_runs WHERE task_id=? ORDER BY reviewer", (task_id,)))
        findings = list(conn.execute("SELECT * FROM findings WHERE task_id=?", (task_id,)))
    assert task["status"] == "success"
    assert task["gate_status"] == "failed"
    assert set(reviewer.calls) == {"correctness", "security", "api_contract", "config_release", "performance", "judge"}
    assert [(row["reviewer"], row["session_id"]) for row in runs] == [
        ("api_contract", "ses_api_contract"),
        ("config_release", "ses_config_release"),
        ("correctness", "ses_correctness"),
        ("cr_judge", "ses_cr_judge"),
        ("performance", "ses_performance"),
        ("security", "ses_security"),
    ]
    assert findings[0]["severity"] == "fatal"
    assert findings[0]["finding_id"].startswith("fnd_")
    report_html = (settings.report_dir / task_id / "index.html").read_text(encoding="utf-8")
    assert "问题列表 (1)" in report_html
    assert "Blocking · fatal (1)" in report_html
    assert "reviewer: security" in report_html
    assert "Token leak" in report_html
    assert "src/auth/token_service.py:1" in report_html


def test_full_workflow_runs_required_reviewers_in_parallel_fanout(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "src/auth/token_service.py", "print('token')\n")
    settings = _settings(tmp_path)
    settings.review_v2_reviewer_concurrency = 3
    db = ReviewDB(settings.review_v2_db_path)
    db.init()
    task_id = db.create_task(task_id="task1", app_name="demo", repo_url=str(repo), branch="feature/security")
    context_dir = tmp_path / "audit" / task_id / "context"
    context_dir.mkdir(parents=True)
    (context_dir / "diff.patch").write_text("diff --git a/src/auth/token_service.py b/src/auth/token_service.py\n", encoding="utf-8")
    (context_dir / "changed_files.json").write_text(json.dumps(["src/auth/token_service.py"]), encoding="utf-8")
    (context_dir / "changed_lines.json").write_text(json.dumps({"src/auth/token_service.py": [1]}), encoding="utf-8")
    (context_dir / "ci_request.json").write_text(json.dumps({"app_name": "demo"}), encoding="utf-8")
    reviewer = BlockingReviewer(expected_parallel=3)
    runner = WorkflowRunner(settings, db, reviewer_runner=reviewer)

    runner._run_reviewers_and_judge(
        task_id,
        repo,
        context_dir,
        {"src/auth/token_service.py": [1]},
        RiskResult(tier="full", reasons=["large changed-line count"], specialists=[]),
        [
            {"reviewer": "correctness", "required": True, "reason": "baseline"},
            {"reviewer": "security", "required": True, "reason": "auth-sensitive path"},
            {"reviewer": "api_contract", "required": True, "reason": "api change"},
            {"reviewer": "config_release", "required": True, "reason": "release risk"},
            {"reviewer": "performance", "required": True, "reason": "large diff"},
        ],
    )

    with db.connect() as conn:
        task = conn.execute("SELECT * FROM cr_tasks WHERE task_id=?", (task_id,)).fetchone()
        runs = list(conn.execute("SELECT reviewer, status FROM reviewer_runs WHERE task_id=? ORDER BY reviewer", (task_id,)))
        events = list(conn.execute("SELECT event_type, payload_json FROM task_events WHERE task_id=? ORDER BY id", (task_id,)))
    assert reviewer.max_active == 3
    assert task["status"] == "success"
    assert task["gate_status"] == "passed"
    assert len(runs) == 6
    assert all(row["status"] == "success" for row in runs)
    assert "reviewer_fanout_started" in [row["event_type"] for row in events]
    fanout_event = next(row for row in events if row["event_type"] == "reviewer_fanout_started")
    assert json.loads(fanout_event["payload_json"])["concurrency"] == 3


def test_workflow_skips_optional_reviewer_with_invalid_output(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "src/app.py", "print('hi')\n")
    settings = _settings(tmp_path)
    db = ReviewDB(settings.review_v2_db_path)
    db.init()
    task_id = db.create_task(task_id="task1", app_name="demo", repo_url=str(repo), branch="feature/app")
    context_dir = tmp_path / "audit" / task_id / "context"
    context_dir.mkdir(parents=True)
    (context_dir / "diff.patch").write_text("diff --git a/src/app.py b/src/app.py\n", encoding="utf-8")
    (context_dir / "changed_files.json").write_text(json.dumps(["src/app.py"]), encoding="utf-8")
    (context_dir / "changed_lines.json").write_text(json.dumps({"src/app.py": [1]}), encoding="utf-8")
    (context_dir / "ci_request.json").write_text(json.dumps({"app_name": "demo"}), encoding="utf-8")
    reviewer = ReviewerMap(
        {
            "correctness": TurnResult(
                type="completed",
                result='{"summary":"ok","pass_check":true,"score":100,"findings":[]}',
                session_id="ses_correctness",
                tokens={"total": 1},
            ),
            "performance": TurnResult(type="completed", result="{}", session_id="ses_performance", tokens={"total": 1}),
            "judge": _judge_turn([]),
        }
    )
    runner = WorkflowRunner(settings, db, reviewer_runner=reviewer)

    runner._run_reviewers_and_judge(
        task_id,
        repo,
        context_dir,
        {"src/app.py": [1]},
        RiskResult(tier="standard", reasons=["standard"], specialists=[]),
        [
            {"reviewer": "correctness", "required": True, "reason": "standard production diff"},
            {"reviewer": "performance", "required": False, "reason": "optional trigger"},
        ],
    )

    with db.connect() as conn:
        task = conn.execute("SELECT * FROM cr_tasks WHERE task_id=?", (task_id,)).fetchone()
        runs = list(conn.execute("SELECT reviewer, status FROM reviewer_runs WHERE task_id=? ORDER BY reviewer", (task_id,)))
    assert task["status"] == "success"
    assert task["gate_status"] == "passed"
    assert [(row["reviewer"], row["status"]) for row in runs] == [
        ("correctness", "success"),
        ("cr_judge", "success"),
        ("performance", "success"),
    ]

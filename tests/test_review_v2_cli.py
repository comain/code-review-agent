from io import StringIO
from pathlib import Path

from cr_agent.config import Settings
from cr_agent.review_v2.cli import build_parser, main
from cr_agent.review_v2.storage import ReviewDB


def _settings(tmp_path: Path) -> Settings:
    return Settings(base_dir=tmp_path / "runtime", review_v2_db_path=tmp_path / "review.sqlite3")


def test_cli_parser_exposes_daemon_operational_parameters() -> None:
    parser = build_parser()

    args = parser.parse_args(
        [
            "run",
            "--daemon-id",
            "d1",
            "--concurrency",
            "2",
            "--poll-interval",
            "0.5",
            "--claim-limit",
            "3",
            "--lease-seconds",
            "60",
            "--once",
        ]
    )

    assert args.command == "run"
    assert args.daemon_id == "d1"
    assert args.concurrency == 2
    assert args.poll_interval == 0.5
    assert args.claim_limit == 3
    assert args.lease_seconds == 60
    assert args.once is True


def test_status_and_dashboard_render_queue_state_without_mutation(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    db = ReviewDB(settings.review_v2_db_path)
    db.init()
    db.create_task(task_id="task1", app_name="demo", repo_url=str(tmp_path), branch="feature/a")
    db.upsert_heartbeat(runner_id="daemon-a", task_id=None, status="IDLE", message="idle")
    before = db.task_counts()
    output = StringIO()

    assert main(["status"], settings=settings, db=db, output=output) == 0
    assert main(["dashboard", "--once"], settings=settings, db=db, output=output) == 0

    text = output.getvalue()
    assert "queued=1" in text
    assert "daemon-a" in text
    assert db.task_counts() == before


def test_cli_stop_cancel_and_requeue_task(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    db = ReviewDB(settings.review_v2_db_path)
    db.init()
    db.create_task(task_id="task1", app_name="demo", repo_url=str(tmp_path), branch="feature/a")
    db.claim_next_task(daemon_id="daemon-a", lease_seconds=60)
    output = StringIO()

    assert main(["stop", "task1", "--reason", "pause"], settings=settings, db=db, output=output) == 0
    assert db.check_task_control("task1") == ("stop", "pause")

    assert main(["cancel", "task1", "--reason", "abort"], settings=settings, db=db, output=output) == 0
    with db.connect() as conn:
        task = conn.execute("SELECT status, gate_status FROM cr_tasks WHERE task_id='task1'").fetchone()
    assert task["status"] == "cancelled"
    assert task["gate_status"] == "cancelled"

    assert main(["requeue", "task1", "--reason", "retry"], settings=settings, db=db, output=output) == 0
    with db.connect() as conn:
        task = conn.execute("SELECT status, gate_status, claimed_by FROM cr_tasks WHERE task_id='task1'").fetchone()
    assert task["status"] == "queued"
    assert task["gate_status"] is None
    assert task["claimed_by"] is None
    text = output.getvalue()
    assert "stop_requested=task1" in text
    assert "cancelled=task1" in text
    assert "requeued=task1" in text

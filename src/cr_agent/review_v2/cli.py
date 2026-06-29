"""CR v2 daemon CLI adapted from comain/unit-test-agent `reference/cli.py` task daemon/dashboard commands."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional, Sequence, TextIO

from cr_agent.config import Settings
from cr_agent.review_v2.daemon import CRReviewDaemon
from cr_agent.review_v2.storage import ReviewDB


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cr-review-v2")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run")
    _add_daemon_options(run)
    run.add_argument("--once", action="store_true")

    once = sub.add_parser("once")
    _add_daemon_options(once)

    recover = sub.add_parser("recover-stale")
    _add_daemon_options(recover)

    retry = sub.add_parser("retry-callbacks")
    _add_daemon_options(retry)

    status = sub.add_parser("status")
    _add_daemon_options(status)

    dashboard = sub.add_parser("dashboard")
    _add_daemon_options(dashboard)
    dashboard.add_argument("--once", action="store_true")

    stop = sub.add_parser("stop")
    _add_task_control_options(stop)

    cancel = sub.add_parser("cancel")
    _add_task_control_options(cancel)

    requeue = sub.add_parser("requeue")
    _add_task_control_options(requeue)
    return parser


def _add_task_control_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("task_id")
    parser.add_argument("--reason", default=None)
    parser.add_argument("--task-db", default=None)


def _add_daemon_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--daemon-id", default=None)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--poll-interval", type=float, default=None)
    parser.add_argument("--claim-limit", type=int, default=None)
    parser.add_argument("--lease-seconds", type=int, default=None)
    parser.add_argument("--task-db", default=None)


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    settings: Optional[Settings] = None,
    db: Optional[ReviewDB] = None,
    workflow_runner=None,
    output: Optional[TextIO] = None,
) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    output = output or sys.stdout
    settings = settings or Settings()
    if getattr(args, "claim_limit", None) is not None:
        settings.review_v2_daemon_claim_limit = args.claim_limit
    if getattr(args, "lease_seconds", None) is not None:
        settings.review_v2_daemon_lease_seconds = args.lease_seconds
    if getattr(args, "task_db", None):
        settings.review_v2_db_path = args.task_db
    db = db or ReviewDB(settings.review_v2_db_path)
    daemon = CRReviewDaemon(settings, db=db, workflow_runner=workflow_runner, daemon_id=getattr(args, "daemon_id", None))

    if args.command == "once":
        output.write(f"claimed={daemon.once()}\n")
    elif args.command == "run":
        daemon.run_forever(poll_interval=args.poll_interval, once=bool(args.once))
        output.write("run=stopped\n")
    elif args.command == "recover-stale":
        output.write("recovered=" + ",".join(daemon.recover_stale()) + "\n")
    elif args.command == "retry-callbacks":
        output.write("due_callbacks=" + ",".join(daemon.retry_callbacks()) + "\n")
    elif args.command == "status":
        output.write(json.dumps(daemon.status_payload(), ensure_ascii=False, sort_keys=True) + "\n")
    elif args.command == "dashboard":
        _render_dashboard(daemon.status_payload(), output)
    elif args.command == "stop":
        db.request_task_stop(args.task_id, reason=args.reason)
        output.write(f"stop_requested={args.task_id}\n")
    elif args.command == "cancel":
        db.cancel_task(args.task_id, reason=args.reason)
        output.write(f"cancelled={args.task_id}\n")
    elif args.command == "requeue":
        db.requeue_task(args.task_id, reason=args.reason)
        output.write(f"requeued={args.task_id}\n")
    return 0


def _render_dashboard(payload: dict, output: TextIO) -> None:
    counts = payload.get("counts") or {}
    parts = [f"{name}={counts.get(name, 0)}" for name in ("queued", "running", "success", "failed", "skipped", "cancelled")]
    output.write("CR v2 dashboard " + " ".join(parts) + "\n")
    for heartbeat in payload.get("heartbeats") or []:
        output.write(
            f"heartbeat {heartbeat.get('runner_id')} {heartbeat.get('status')} {heartbeat.get('message') or ''}\n"
        )
    for task in payload.get("active_tasks") or []:
        output.write(f"task {task.get('task_id')} {task.get('status')} {task.get('gate_status') or ''}\n")


if __name__ == "__main__":
    raise SystemExit(main())

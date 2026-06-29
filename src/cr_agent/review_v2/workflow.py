"""Initial skipped/light CR v2 workflow path."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import html
import json
from pathlib import Path
from typing import Any, Dict, Optional

from cr_agent.config import Settings
from cr_agent.core.git_client import GitClient
from cr_agent.review_v2.context import ContextBuilder
from cr_agent.review_v2.costing import cost_from_provider_or_tokens
from cr_agent.review_v2.guards import prepare_review_guard
from cr_agent.review_v2.judge import JudgeNormalizer
from cr_agent.review_v2.json_output import extract_json_object
from cr_agent.review_v2.opencode_config import selected_opencode_model
from cr_agent.review_v2.opencode_process import OpenCodeProcessRunner, TurnResult
from cr_agent.review_v2.prompts import PromptRenderer
from cr_agent.review_v2.risk import RiskResult, ReviewerPlanItem
from cr_agent.review_v2.storage import ReviewDB


_SEVERITY_ORDER = ("fatal", "high", "medium", "low", "info")
_BLOCKING_SEVERITIES = {"fatal", "high", "medium"}


def _finding_severity(item: Dict[str, Any]) -> str:
    severity = str(item.get("effective_severity") or item.get("severity") or item.get("original_severity") or "info").lower()
    return severity if severity in _SEVERITY_ORDER else "info"


def _finding_location(item: Dict[str, Any]) -> str:
    path = str(item.get("file_path") or item.get("file") or "-")
    line = item.get("line")
    return f"{path}:{line}" if line else path


def _severity_rank(item: Dict[str, Any]) -> int:
    try:
        return _SEVERITY_ORDER.index(_finding_severity(item))
    except ValueError:
        return len(_SEVERITY_ORDER)


def _severity_group_title(severity: str, count: int) -> str:
    layer = "Blocking" if severity in _BLOCKING_SEVERITIES else "Non-blocking"
    return f"{layer} · {severity} ({count})"


def _render_static_findings(findings: list[Dict[str, Any]]) -> str:
    if not findings:
        return "<p>未发现问题。</p>"
    grouped: Dict[str, list[Dict[str, Any]]] = {}
    for item in sorted(findings, key=lambda finding: (_severity_rank(finding), _finding_location(finding))):
        grouped.setdefault(_finding_severity(item), []).append(item)
    sections: list[str] = []
    for severity in _SEVERITY_ORDER:
        items = grouped.get(severity) or []
        if not items:
            continue
        layer = "blocking" if severity in _BLOCKING_SEVERITIES else "non-blocking"
        body = "\n".join(_render_static_finding_card(item, index) for index, item in enumerate(items, start=1))
        sections.append(
            f"""
            <section class="severity-group severity-{severity}">
              <h3>{html.escape(_severity_group_title(severity, len(items)))}</h3>
              {body}
            </section>
            """
        )
    return "\n".join(sections)


def _render_static_finding_card(item: Dict[str, Any], index: int) -> str:
    severity = _finding_severity(item)
    layer_label = "Blocking" if severity in _BLOCKING_SEVERITIES else "Non-blocking"
    layer_class = "blocking" if severity in _BLOCKING_SEVERITIES else "non-blocking"
    reviewer = str(item.get("source_reviewer") or "")
    reviewer_badge = f'<span class="badge">reviewer: {html.escape(reviewer)}</span>' if reviewer else ""
    return f"""
    <article class="finding severity-{severity}">
      <h3>{index}. {html.escape(str(item.get("title") or "未命名问题"))}</h3>
      <p>
        <span class="badge">{html.escape(severity)}</span>
        <span class="badge {layer_class}">{html.escape(layer_label)}</span>
        <span class="badge">{html.escape(str(item.get("status") or "open"))}</span>
        {reviewer_badge}
        <code>{html.escape(_finding_location(item))}</code>
      </p>
      <p>{html.escape(str(item.get("detail") or ""))}</p>
      {f"<pre>{html.escape(str(item.get('suggestion')))}</pre>" if item.get("suggestion") else ""}
    </article>
    """


class TaskControlRequested(RuntimeError):
    def __init__(self, action: str, reason: str):
        super().__init__(reason)
        self.action = action
        self.reason = reason


class WorkflowRunner:
    def __init__(self, settings: Settings, db: ReviewDB, *, reviewer_runner: Any = None, repo_preparer: Any = None):
        self.settings = settings
        self.db = db
        self.reviewer_runner = reviewer_runner or OpenCodeProcessRunner(settings)
        self.repo_preparer = repo_preparer
        self.context_builder = ContextBuilder(settings)
        self.prompt_renderer = PromptRenderer(settings)
        self.judge = JudgeNormalizer()

    def run(self, task_id: str) -> None:
        task = self.db.get_task(task_id)
        if task is None:
            raise RuntimeError(f"task not found: {task_id}")
        try:
            git = GitClient(self.settings)
            self._raise_if_task_control_requested(task_id)
            repo_path = self._prepare_repo(task, git_client=git)
            resolved_commit_id = git.current_commit(repo_path, task_id=task_id)
            if resolved_commit_id != (task["commit_id"] or ""):
                self.db.update_task_commit_id(task_id, resolved_commit_id)
                task = self.db.get_task(task_id) or task
            self._raise_if_task_control_requested(task_id)
            review_context = self._collect_review_context(task_id, repo_path, git)
            diff_range = str(review_context.get("diff_range") or "HEAD~1..HEAD")
            prepared = self.context_builder.prepare(
                task_id=task_id,
                repo_path=repo_path,
                request={
                    "app_name": task["app_name"],
                    "branch": task["branch"],
                    "repo_url": task["repo_url"],
                    "commit_id": resolved_commit_id,
                    "diff_range": diff_range,
                    "commit_log": review_context.get("commit_log") or "",
                },
                diff_range=diff_range,
            )
            risk_data = json.loads((prepared.context_dir / "risk.json").read_text(encoding="utf-8"))
            plan_data = json.loads((prepared.context_dir / "reviewer_plan.json").read_text(encoding="utf-8"))
            risk = RiskResult(
                tier=risk_data["tier"],
                reasons=risk_data.get("reasons") or [],
                specialists=risk_data.get("specialists") or [],
                context_too_large=bool(risk_data.get("context_too_large")),
            )
            guard = prepare_review_guard(risk)
            self._raise_if_task_control_requested(task_id)
            for item in plan_data:
                self.db.create_reviewer_plan(
                    task_id=task_id,
                    workflow_run_id="wf1",
                    reviewer=item["reviewer"],
                    required=bool(item["required"]),
                    risk_tier=risk.tier,
                    reason=item.get("reason"),
                )
            if guard["outcome"] == "skipped":
                reason_text = ", ".join(risk.reasons) if risk.reasons else "no reviewable production files"
                report_url = self._write_report(
                    task_id,
                    {
                        "task_id": task_id,
                        "status": "success",
                        "gate_status": "skipped",
                        "summary": f"No reviewable production files ({reason_text}); LLM review skipped.",
                        "findings": [],
                    },
                )
                self.db.update_task_outcome(task_id, status="success", gate_status="skipped", report_url=report_url)
                return
            if guard["outcome"] == "fail":
                self.db.update_task_outcome(
                    task_id,
                    status="failed",
                    gate_status=guard["gate_status"],
                    error=guard["gate_status"],
                )
                return
            self._run_reviewers_and_judge(task_id, repo_path, prepared.context_dir, prepared.changed_lines, risk, plan_data)
        except TaskControlRequested as exc:
            if exc.action == "cancel":
                self.db.cancel_task(task_id, reason=exc.reason)
            else:
                self.db.mark_task_stopped(task_id, reason=exc.reason)
        except Exception as exc:
            self.db.update_task_outcome(task_id, status="failed", gate_status="incomplete", error=str(exc))

    def _prepare_repo(self, task: Any, *, git_client: Optional[GitClient] = None) -> Path:
        repo_url = task["repo_url"]
        local_path = Path(repo_url)
        if local_path.exists() and (local_path / ".git").exists():
            return local_path
        if self.repo_preparer is not None:
            return Path(
                self.repo_preparer(
                    repo_url=repo_url,
                    branch=task["branch"],
                    commit_id=task["commit_id"],
                    task_id=task["task_id"],
                )
            )
        git = git_client or GitClient(self.settings)
        return git.prepare_repo(
            repo_url=repo_url,
            branch=task["branch"],
            commit_id=task["commit_id"],
            task_id=task["task_id"],
        )

    def _collect_review_context(self, task_id: str, repo_path: Path, git: GitClient) -> Dict[str, Any]:
        try:
            review_context = git.collect_review_context(repo_path, task_id=task_id)
        except Exception as exc:  # noqa: BLE001
            self.db.add_task_event(
                task_id,
                "diff_range_fallback",
                "failed to resolve branch diff range; falling back to last commit",
                stage="prepare_context",
                severity="warn",
                payload={"fallback_diff_range": "HEAD~1..HEAD", "error": str(exc)},
            )
            return {"diff_range": "HEAD~1..HEAD", "commit_log": ""}
        self.db.add_task_event(
            task_id,
            "diff_range_resolved",
            "resolved review diff range",
            stage="prepare_context",
            payload={"diff_range": review_context.get("diff_range")},
        )
        return review_context

    def _run_reviewers_and_judge(
        self,
        task_id: str,
        repo_path: Path,
        context_dir: Path,
        changed_lines: Dict[str, list],
        risk: RiskResult,
        plan_data: list,
    ) -> None:
        self._raise_if_task_control_requested(task_id)
        required = [item for item in plan_data if item.get("required")]
        if not required:
            raise RuntimeError("production review requires at least one required reviewer")
        reviewers = [
            ReviewerPlanItem(
                reviewer=item["reviewer"],
                required=bool(item.get("required")),
                reason=item.get("reason") or "reviewer",
            )
            for item in plan_data
        ]
        max_workers = max(1, min(len(reviewers), int(self.settings.review_v2_reviewer_concurrency or 1)))
        self.db.add_task_event(
            task_id,
            "reviewer_fanout_started",
            f"{risk.tier} review fanout started",
            stage="reviewer_fanout",
            payload={"risk_tier": risk.tier, "reviewers": [item.reviewer for item in reviewers], "concurrency": max_workers},
        )
        reviewer_outputs = []
        failures: list[str] = []
        control_request: TaskControlRequested | None = None
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix=f"cr-v2-review-{task_id[:8]}") as executor:
            futures = {
                executor.submit(
                    self._run_single_reviewer,
                    task_id=task_id,
                    repo_path=repo_path,
                    context_dir=context_dir,
                    risk=risk,
                    reviewer=reviewer,
                ): reviewer
                for reviewer in reviewers
            }
            for future in as_completed(futures):
                reviewer = futures[future]
                try:
                    reviewer_json = future.result()
                except TaskControlRequested as exc:
                    control_request = exc
                    continue
                except Exception as exc:  # noqa: BLE001
                    if reviewer.required:
                        failures.append(f"{reviewer.reviewer}: {exc}")
                    continue
                if reviewer_json:
                    reviewer_outputs.append(reviewer_json)
        if control_request is not None:
            raise control_request
        self._raise_if_task_control_requested(task_id)
        if failures:
            raise RuntimeError("required reviewer failed: " + "; ".join(failures))
        self.db.add_task_event(
            task_id,
            "reviewer_fanout_completed",
            f"{risk.tier} review fanout completed",
            stage="reviewer_fanout",
            payload={"risk_tier": risk.tier, "outputs": len(reviewer_outputs)},
        )
        judge_result = self._run_judge_session(
            task_id=task_id,
            repo_path=repo_path,
            context_dir=context_dir,
            reviewer_outputs=reviewer_outputs,
            changed_lines=changed_lines,
            risk=risk,
        )
        judge_dir = context_dir.parent / "judge"
        (judge_dir / "rejections.json").write_text(
            json.dumps(judge_result.rejected_candidates, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        for finding in judge_result.accepted_findings:
            self._raise_if_task_control_requested(task_id)
            self.db.create_finding(
                task_id=task_id,
                reviewer_run_id=finding.get("source_review_run_id"),
                file_path=finding["file_path"],
                line=finding.get("line"),
                severity=finding["severity"],
                title=finding["title"],
                detail=finding["detail"],
                suggestion=finding.get("suggestion"),
                finding_id=finding["finding_id"],
            )
        gate_status = "failed" if judge_result.accepted_findings else "passed"
        self._raise_if_task_control_requested(task_id)
        report_url = self._write_report(
            task_id,
            {
                "task_id": task_id,
                "status": "success",
                "gate_status": gate_status,
                "summary": "Review found issues." if judge_result.accepted_findings else "Review passed.",
                "findings": judge_result.accepted_findings,
            },
        )
        self.db.update_task_outcome(task_id, status="success", gate_status=gate_status, report_url=report_url)

    def _run_judge_session(
        self,
        *,
        task_id: str,
        repo_path: Path,
        context_dir: Path,
        reviewer_outputs: list[Dict[str, Any]],
        changed_lines: Dict[str, list],
        risk: RiskResult,
    ):
        prompt = self.prompt_renderer.render_judge_prompt(
            task_id=task_id,
            context_dir=context_dir,
            risk=risk,
            reviewer_outputs=reviewer_outputs,
        )
        output_path = context_dir.parent / "judge" / "output.json"
        selected_model = selected_opencode_model(self.settings)
        run_id = self.db.start_reviewer_run(
            task_id=task_id,
            reviewer="cr_judge",
            workflow_run_id="wf1",
            attempt=1,
            model_id=selected_model,
        )
        self.db.add_task_event(
            task_id,
            "judge_started",
            "cr_judge started",
            stage="judge",
            payload={"reviewer": "cr_judge", "run_id": run_id},
        )
        try:
            result: TurnResult = self.reviewer_runner.run_turn(
                prompt_file=prompt.path,
                repo_path=repo_path,
                model_id=selected_model,
                is_cancelled=lambda: self._task_control_requested(task_id),
            )
        except Exception as exc:
            self._raise_if_task_control_requested(task_id)
            self.db.finish_reviewer_run(
                run_id,
                status="failed",
                output_path=str(output_path),
                error=str(exc),
                model_id=selected_model,
            )
            self.db.add_task_event(
                task_id,
                "judge_failed",
                "cr_judge failed",
                stage="judge",
                severity="error",
                payload={"reviewer": "cr_judge", "run_id": run_id, "error": str(exc)},
            )
            raise
        self._raise_if_task_control_requested(task_id)
        output_path.write_text(result.result or "{}", encoding="utf-8")
        status = "success" if result.type == "completed" and result.session_id else "failed"
        self.db.finish_reviewer_run(
            run_id,
            status=status,
            session_id=result.session_id,
            raw_log_path=result.raw_log_path,
            output_path=str(output_path),
            error=None if status == "success" else "missing session id or judge failed",
            model_id=result.model_id or selected_model,
            token_usage=self._token_usage(result, selected_model),
        )
        if status != "success":
            self.db.add_task_event(
                task_id,
                "judge_failed",
                "cr_judge failed",
                stage="judge",
                severity="error",
                payload={"reviewer": "cr_judge", "run_id": run_id, "error": "missing session id or judge failed"},
            )
            raise RuntimeError("cr_judge failed: missing session id or judge failed")
        try:
            judge_json = extract_json_object(result.result or "{}", required_keys=("accepted_findings", "rejected_candidates"))
        except json.JSONDecodeError as exc:
            self.db.add_task_event(
                task_id,
                "judge_parse_failed",
                "cr_judge returned invalid JSON",
                stage="judge",
                severity="error",
                payload={"reviewer": "cr_judge", "run_id": run_id, "error": str(exc)},
            )
            raise RuntimeError(f"cr_judge returned invalid JSON: {exc}") from exc
        guarded_outputs = self._judge_payload_to_reviewer_outputs(judge_json)
        judge_result = self.judge.normalize(
            reviewer_outputs=guarded_outputs,
            changed_lines=changed_lines,
            risk_tier=risk.tier,
        )
        (context_dir.parent / "judge" / "judge.json").write_text(
            json.dumps(judge_json, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.db.add_task_event(
            task_id,
            "judge_completed",
            "cr_judge completed",
            stage="judge",
            payload={"reviewer": "cr_judge", "run_id": run_id, "accepted_findings": len(judge_result.accepted_findings)},
        )
        return judge_result

    @staticmethod
    def _judge_payload_to_reviewer_outputs(judge_json: Dict[str, Any]) -> list[Dict[str, Any]]:
        grouped: dict[tuple[Any, Any], list[Dict[str, Any]]] = {}
        for item in judge_json.get("accepted_findings") or []:
            reviewer = item.get("source_reviewer") or "cr_judge"
            review_run_id = item.get("source_review_run_id")
            grouped.setdefault((reviewer, review_run_id), []).append(
                {
                    "file": item.get("file_path") or item.get("file"),
                    "line": item.get("line"),
                    "severity": item.get("severity"),
                    "title": item.get("title"),
                    "detail": item.get("detail"),
                    "suggestion": item.get("suggestion"),
                    "confidence": item.get("confidence", 1.0),
                }
            )
        return [
            {"review_run_id": review_run_id, "reviewer": reviewer, "findings": findings}
            for (reviewer, review_run_id), findings in grouped.items()
        ]

    def _run_single_reviewer(
        self,
        *,
        task_id: str,
        repo_path: Path,
        context_dir: Path,
        risk: RiskResult,
        reviewer: ReviewerPlanItem,
    ) -> Dict[str, Any] | None:
        self._raise_if_task_control_requested(task_id)
        prompt = self.prompt_renderer.render_reviewer_prompt(
            task_id=task_id,
            context_dir=context_dir,
            risk=risk,
            reviewer=reviewer,
        )
        output_path = context_dir.parent / "reviewers" / reviewer.reviewer / "output.json"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        selected_model = selected_opencode_model(self.settings)
        run_id = self.db.start_reviewer_run(
            task_id=task_id,
            reviewer=reviewer.reviewer,
            workflow_run_id="wf1",
            attempt=1,
            model_id=selected_model,
        )
        self.db.add_task_event(
            task_id,
            "reviewer_started",
            f"{reviewer.reviewer} started",
            stage=f"reviewer:{reviewer.reviewer}",
            payload={"reviewer": reviewer.reviewer, "required": reviewer.required, "run_id": run_id},
        )
        try:
            try:
                result: TurnResult = self.reviewer_runner.run_turn(
                    prompt_file=prompt.path,
                    repo_path=repo_path,
                    model_id=selected_model,
                    is_cancelled=lambda: self._task_control_requested(task_id),
                )
            except Exception as exc:
                self._raise_if_task_control_requested(task_id)
                self.db.finish_reviewer_run(
                    run_id,
                    status="failed",
                    output_path=str(output_path),
                    error=str(exc),
                    model_id=selected_model,
                )
                self.db.add_task_event(
                    task_id,
                    "reviewer_failed",
                    f"{reviewer.reviewer} failed",
                    stage=f"reviewer:{reviewer.reviewer}",
                    severity="error",
                    payload={"reviewer": reviewer.reviewer, "required": reviewer.required, "run_id": run_id, "error": str(exc)},
                )
                raise
            self._raise_if_task_control_requested(task_id)
            output_path.write_text(result.result or "{}", encoding="utf-8")
            status = "success" if result.type == "completed" and result.session_id else "failed"
            self.db.finish_reviewer_run(
                run_id,
                status=status,
                session_id=result.session_id,
                raw_log_path=result.raw_log_path,
                output_path=str(output_path),
                error=None if status == "success" else "missing session id or reviewer failed",
                model_id=result.model_id or selected_model,
                token_usage=self._token_usage(result, selected_model),
            )
            self.db.add_task_event(
                task_id,
                "reviewer_completed" if status == "success" else "reviewer_failed",
                f"{reviewer.reviewer} {status}",
                stage=f"reviewer:{reviewer.reviewer}",
                severity="info" if status == "success" else "error",
                payload={
                    "reviewer": reviewer.reviewer,
                    "required": reviewer.required,
                    "run_id": run_id,
                    "session_id": result.session_id,
                    "total_tokens": int(result.tokens.get("total") or 0),
                },
            )
            if status != "success":
                self._raise_if_task_control_requested(task_id)
                if reviewer.required:
                    raise RuntimeError("required reviewer failed or did not return session id")
                return None
            try:
                reviewer_json = extract_json_object(result.result or "{}", required_keys=("findings",))
            except json.JSONDecodeError as exc:
                if reviewer.required:
                    raise RuntimeError("invalid reviewer output") from exc
                self.db.add_task_event(
                    task_id,
                    "reviewer_output_skipped",
                    f"{reviewer.reviewer} output missing findings",
                    stage=f"reviewer:{reviewer.reviewer}",
                    severity="warn",
                    payload={"reviewer": reviewer.reviewer, "required": reviewer.required, "run_id": run_id, "error": str(exc)},
                )
                return None
            if not isinstance(reviewer_json, dict) or "findings" not in reviewer_json:
                if reviewer.required:
                    raise RuntimeError("invalid reviewer output")
                self.db.add_task_event(
                    task_id,
                    "reviewer_output_skipped",
                    f"{reviewer.reviewer} output missing findings",
                    stage=f"reviewer:{reviewer.reviewer}",
                    severity="warn",
                    payload={"reviewer": reviewer.reviewer, "run_id": run_id},
                )
                return None
            reviewer_json["review_run_id"] = run_id
            reviewer_json["reviewer"] = reviewer.reviewer
            return reviewer_json
        except Exception:
            raise

    def _task_control_requested(self, task_id: str) -> bool:
        try:
            self._raise_if_task_control_requested(task_id)
        except TaskControlRequested:
            return True
        return False

    def _raise_if_task_control_requested(self, task_id: str) -> None:
        task = self.db.get_task(task_id)
        if task is not None and task["status"] == "cancelled":
            gate_status = task["gate_status"] or "cancelled"
            action = "stop" if gate_status == "stopped" else "cancel"
            raise TaskControlRequested(action, task["error"] or f"{action} requested")
        control = self.db.check_task_control(task_id)
        if control is None:
            return
        action, reason = control
        raise TaskControlRequested(action, reason)

    def _write_report(self, task_id: str, payload: Dict[str, Any]) -> str:
        report_dir = self.settings.report_dir / task_id
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "result.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        findings = payload.get("findings") or []
        findings_html = _render_static_findings(findings)
        html_content = (
            "<!doctype html><meta charset='utf-8'>"
            "<style>"
            "body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;margin:32px;color:#172033;background:#f6f8fb}"
            "article,section{background:#fff}"
            ".severity-group{border:1px solid #d9e1ec;border-radius:8px;padding:14px;margin:12px 0 18px}"
            ".severity-fatal{border-left:5px solid #991b1b}.severity-high{border-left:5px solid #dc2626}"
            ".severity-medium{border-left:5px solid #d97706}.severity-low{border-left:5px solid #2563eb}.severity-info{border-left:5px solid #64748b}"
            ".finding{border:1px solid #d9e1ec;border-radius:8px;padding:14px;margin:12px 0}"
            ".badge{display:inline-block;border:1px solid #d0d7e2;border-radius:999px;padding:1px 8px;margin-right:6px;font-size:12px}"
            ".blocking{background:#fee2e2;border-color:#fecaca;color:#991b1b}.non-blocking{background:#ecfdf5;border-color:#bbf7d0;color:#047857}"
            "code{word-break:break-all}pre{white-space:pre-wrap;word-break:break-word;background:#f7f7f7;padding:12px;border-radius:8px}"
            "</style>"
            f"<title>CR Report {task_id}</title>"
            f"<h1>CR Report {task_id}</h1>"
            f"<p>Status: {payload['status']} / {payload['gate_status']}</p>"
            f"<p>{payload['summary']}</p>"
            f"<h2>问题列表 ({len(findings)})</h2>"
            f"{findings_html}"
        )
        (report_dir / "index.html").write_text(html_content, encoding="utf-8")
        return f"{self.settings.report_base_url.rstrip('/')}/{task_id}/index.html"

    @staticmethod
    def _token_usage(result: TurnResult, default_model: str | None = None) -> Dict[str, Any]:
        cache = result.tokens.get("cache") or {}
        input_tokens = int(result.tokens.get("input") or 0)
        output_tokens = int(result.tokens.get("output") or 0)
        reasoning_tokens = int(result.tokens.get("reasoning") or 0)
        cache_read_tokens = int(cache.get("read") or 0)
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "reasoning_tokens": reasoning_tokens,
            "cache_read_tokens": cache_read_tokens,
            "cache_write_tokens": int(cache.get("write") or 0),
            "total_tokens": int(result.tokens.get("total") or 0),
            "cost_usd": cost_from_provider_or_tokens(
                provider_cost_usd=result.cost_usd,
                model=result.model_id or default_model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens,
                reasoning_tokens=reasoning_tokens,
            ),
        }

"""Finding feedback sessions for CR v2."""

from __future__ import annotations

import json
from pathlib import Path
import threading
from typing import Any, Optional

from cr_agent.config import Settings
from cr_agent.review_v2.costing import cost_from_provider_or_tokens
from cr_agent.review_v2.json_output import extract_json_object
from cr_agent.review_v2.opencode_process import OpenCodeProcessRunner, TurnResult
from cr_agent.review_v2.reviewer_profiles import reviewer_profile
from cr_agent.review_v2.storage import ReviewDB


MODEL_RESOLVED_STATUSES = {"resolved_model_false_positive", "re_reviewed_pass"}
HUMAN_RESOLVED_STATUS = "human_non_fix"


class FeedbackProcessor:
    def __init__(self, settings: Settings, db: ReviewDB, *, runner: Optional[Any] = None):
        self.settings = settings
        self.db = db
        self.runner = runner or OpenCodeProcessRunner(settings)

    def submit_finding_feedback(self, *, task_id: str, finding_id: str, message: str) -> str:
        feedback_id, prompt_path = self.start_finding_feedback(task_id=task_id, finding_id=finding_id, message=message)
        self.process_finding_feedback(
            task_id=task_id,
            finding_id=finding_id,
            feedback_session_id=feedback_id,
            prompt_path=prompt_path,
            message=message,
        )
        return feedback_id

    def submit_finding_feedback_background(self, *, task_id: str, finding_id: str, message: str) -> str:
        feedback_id, prompt_path = self.start_finding_feedback(task_id=task_id, finding_id=finding_id, message=message)
        thread = threading.Thread(
            target=self.process_finding_feedback,
            kwargs={
                "task_id": task_id,
                "finding_id": finding_id,
                "feedback_session_id": feedback_id,
                "prompt_path": prompt_path,
                "message": message,
            },
            name=f"cr-v2-feedback-{feedback_id[:12]}",
            daemon=True,
        )
        thread.start()
        return feedback_id

    def start_finding_feedback(self, *, task_id: str, finding_id: str, message: str) -> tuple[str, Path]:
        finding = self.db.get_finding_with_reviewer(task_id=task_id, finding_id=finding_id)
        if finding is None:
            raise ValueError(f"finding not found for task: {finding_id}")
        prompt_path = self._write_prompt(task_id, finding, message)
        feedback_id = self.db.create_feedback_session(
            task_id=task_id,
            finding_id=finding_id,
            parent_reviewer_run_id=finding["source_reviewer_run_id"],
            status="running",
            feedback_text=message,
        )
        self.db.add_task_event(
            task_id,
            "feedback_started",
            "feedback re-review started",
            stage="feedback",
            payload={"finding_id": finding_id, "feedback_session_id": feedback_id, "reviewer": finding["source_reviewer"]},
        )
        return feedback_id, prompt_path

    def process_finding_feedback(
        self,
        *,
        task_id: str,
        finding_id: str,
        feedback_session_id: str,
        prompt_path: Path,
        message: str,
    ) -> None:
        try:
            result: TurnResult = self.runner.run_turn(
                prompt_file=prompt_path,
                repo_path=prompt_path.parent,
            )
        except Exception as exc:  # noqa: BLE001
            self.db.finish_feedback_session(feedback_session_id, status="failed")
            self.db.add_task_event(
                task_id,
                "feedback_failed",
                "feedback session failed",
                stage="feedback",
                severity="error",
                payload={"finding_id": finding_id, "feedback_session_id": feedback_session_id, "error": str(exc)},
            )
            return
        payload: dict[str, Any] = {}
        parse_error: Optional[str] = None
        if result.result:
            try:
                payload = extract_json_object(result.result, required_keys=("resolved",))
            except json.JSONDecodeError as exc:
                parse_error = str(exc)
        model_reply = str(payload.get("reply") or "") if payload else None
        resolved = result.type == "completed" and bool(payload.get("resolved"))
        status = self._resolved_status(payload.get("status")) if resolved else "open"
        self.db.finish_feedback_session(
            feedback_session_id,
            status="success" if resolved else "failed",
            opencode_session_id=result.session_id,
            model_reply=model_reply,
            token_usage=self._token_usage(result),
        )
        self.db.add_task_event(
            task_id,
            "feedback_completed" if resolved else "feedback_failed",
            "feedback re-review completed" if resolved else "feedback re-review failed",
            stage="feedback",
            severity="info" if resolved else "warn",
            payload={
                "finding_id": finding_id,
                "feedback_session_id": feedback_session_id,
                "opencode_session_id": result.session_id,
                "status": "success" if resolved else "failed",
                "reply": model_reply,
            },
        )
        if resolved:
            self.db.update_finding_status(
                task_id=task_id,
                finding_id=finding_id,
                status=status,
                actor="model",
                message=payload.get("reply") or message,
            )
            self._recompute_pass_ack(task_id)
        elif parse_error:
            self.db.add_task_event(
                task_id,
                "feedback_parse_failed",
                "feedback session returned invalid JSON",
                stage="feedback",
                severity="warn",
                payload={"finding_id": finding_id, "error": parse_error},
            )

    def mark_human_non_fix(self, *, task_id: str, finding_id: str, actor: str, rationale: str) -> None:
        self.db.update_finding_status(
            task_id=task_id,
            finding_id=finding_id,
            status=HUMAN_RESOLVED_STATUS,
            actor=actor,
            message=rationale,
        )
        self._recompute_pass_ack(task_id)

    def _recompute_pass_ack(self, task_id: str) -> None:
        if self.db.open_findings_count(task_id) != 0:
            return
        self.db.update_task_outcome(task_id, status="success", gate_status="passed")

    @staticmethod
    def _resolved_status(value: Any) -> str:
        status = str(value or "resolved_model_false_positive")
        if status not in MODEL_RESOLVED_STATUSES:
            return "resolved_model_false_positive"
        return status

    def _write_prompt(self, task_id: str, finding: Any, message: str) -> Path:
        finding_id = str(finding["finding_id"])
        reviewer = str(finding["source_reviewer"] or "correctness")
        session_id = str(finding["source_session_id"] or "")
        root = self.settings.review_v2_audit_dir / task_id / "feedback" / finding_id
        root.mkdir(parents=True, exist_ok=True)
        path = root / "prompt.md"
        profile = reviewer_profile(reviewer)
        finding_payload = {
            "finding_id": finding_id,
            "reviewer": reviewer,
            "reviewer_run_id": finding["source_reviewer_run_id"],
            "reviewer_session_id": session_id or None,
            "file": finding["file_path"],
            "line": finding["line"],
            "severity": finding["severity"],
            "title": finding["title"],
            "detail": finding["detail"],
            "suggestion": finding["suggestion"],
        }
        path.write_text(
            "\n".join(
                [
                    f"Review feedback for finding {finding_id}.",
                    "",
                    f"Original contributing reviewer: {reviewer}",
                    f"Original OpenCode session: {session_id or 'unknown'}",
                    "",
                    "Reviewer profile:",
                    json.dumps(profile.to_dict(), ensure_ascii=False, indent=2),
                    "",
                    "Scope:",
                    "- Run this as a focused feedback re-review for the original contributing reviewer only.",
                    "- Do not restart the whole CR workflow or invoke unrelated reviewer personas.",
                    "- Re-check only whether this finding should remain open after the user feedback.",
                    "",
                    "Original finding:",
                    json.dumps(finding_payload, ensure_ascii=False, indent=2),
                    "",
                    "User feedback:",
                    message,
                    "",
                    "Return one JSON object only:",
                    '{"resolved": boolean, "status": "resolved_model_false_positive|re_reviewed_pass|null", "reply": string}',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return path

    @staticmethod
    def _token_usage(result: TurnResult) -> dict:
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
                model=result.model_id or "llm-proxy/gpt-5.5",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens,
                reasoning_tokens=reasoning_tokens,
            ),
        }

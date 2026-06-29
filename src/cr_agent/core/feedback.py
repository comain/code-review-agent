from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from cr_agent.models import FindingFeedbackThread, FixSession, GeneralFeedbackItem


class FeedbackStore:
    def __init__(self, report_dir: Path, issues_dir: Path) -> None:
        self.report_dir = report_dir
        self.issues_dir = issues_dir
        self.issues_dir.mkdir(parents=True, exist_ok=True)

    def load_threads(self, task_id: str) -> Dict[int, FindingFeedbackThread]:
        path = self._path(task_id)
        if not path.exists():
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
        threads = {}
        for item in payload.get("threads", []):
            thread = FindingFeedbackThread.model_validate(item)
            threads[thread.finding_index] = thread
        return threads

    def load_general_feedbacks(self, task_id: str) -> List[GeneralFeedbackItem]:
        path = self._path(task_id)
        if not path.exists():
            return []
        payload = json.loads(path.read_text(encoding="utf-8"))
        return [GeneralFeedbackItem.model_validate(item) for item in payload.get("general_feedbacks", [])]

    def load_fix_sessions(self, task_id: str) -> List[FixSession]:
        path = self._path(task_id)
        if not path.exists():
            return []
        payload = json.loads(path.read_text(encoding="utf-8"))
        return [FixSession.model_validate(item) for item in payload.get("fix_sessions", [])]

    def save_threads(self, task_id: str, threads: Dict[int, FindingFeedbackThread]) -> None:
        general_feedbacks = self.load_general_feedbacks(task_id)
        fix_sessions = self.load_fix_sessions(task_id)
        self._save_feedback_file(task_id, threads, general_feedbacks, fix_sessions)

    def save_general_feedbacks(self, task_id: str, general_feedbacks: List[GeneralFeedbackItem]) -> None:
        threads = self.load_threads(task_id)
        fix_sessions = self.load_fix_sessions(task_id)
        self._save_feedback_file(task_id, threads, general_feedbacks, fix_sessions)

    def save_fix_sessions(self, task_id: str, fix_sessions: List[FixSession]) -> None:
        threads = self.load_threads(task_id)
        general_feedbacks = self.load_general_feedbacks(task_id)
        self._save_feedback_file(task_id, threads, general_feedbacks, fix_sessions)

    def _save_feedback_file(
        self,
        task_id: str,
        threads: Dict[int, FindingFeedbackThread],
        general_feedbacks: List[GeneralFeedbackItem],
        fix_sessions: List[FixSession],
    ) -> None:
        path = self._path(task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "threads": [thread.model_dump(mode="json") for _, thread in sorted(threads.items())],
            "general_feedbacks": [item.model_dump(mode="json") for item in general_feedbacks],
            "fix_sessions": [item.model_dump(mode="json") for item in fix_sessions],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def append_issue_pattern(
        self,
        *,
        task_id: str,
        app_name: str,
        branch: str,
        commit_id: Optional[str],
        finding_index: int,
        file: str,
        title: str,
        action: str,
        severity: Optional[str],
        conversation: list[dict],
        pattern_summary: Optional[str],
    ) -> Path:
        day = datetime.now().strftime("%Y-%m-%d")
        path = self.issues_dir / f"{day}.accepted-finding-feedback.jsonl"
        entry = {
            "task_id": task_id,
            "app_name": app_name,
            "branch": branch,
            "commit_id": commit_id,
            "finding_index": finding_index,
            "file": file,
            "title": title,
            "action": action,
            "severity": severity,
            "pattern_summary": pattern_summary or "",
            "conversation": conversation,
            "created_at": datetime.now().isoformat(),
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return path

    def append_missed_issue_pattern(
        self,
        *,
        task_id: str,
        app_name: str,
        branch: str,
        commit_id: Optional[str],
        content: str,
        reply: str,
        pattern_summary: Optional[str],
    ) -> Path:
        day = datetime.now().strftime("%Y-%m-%d")
        path = self.issues_dir / f"{day}.missed-issue.jsonl"
        entry = {
            "task_id": task_id,
            "app_name": app_name,
            "branch": branch,
            "commit_id": commit_id,
            "content": content,
            "reply": reply,
            "pattern_summary": pattern_summary or "",
            "created_at": datetime.now().isoformat(),
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return path

    def _path(self, task_id: str) -> Path:
        return self.report_dir / task_id / "feedback.json"

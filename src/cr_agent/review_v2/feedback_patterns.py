"""Mine CR v2 feedback events and propose prompt-reference updates by MR."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import json
from pathlib import Path
import os
import shutil
import subprocess
from tempfile import mkdtemp
from typing import Any, Iterable, Optional
import urllib.error
import urllib.parse
import urllib.request

from cr_agent.config import Settings
from cr_agent.review_v2.storage import ReviewDB


RESOLVED_PATTERN_STATUSES = ("resolved_model_false_positive", "re_reviewed_pass", "human_non_fix")
TARGET_PATTERNS_REL = Path("src/cr_agent/review_v2/templates/references/false_positive_patterns.md")
TARGET_REVIEW_REFERENCE_REL = Path("src/cr_agent/review_v2/templates/references/review.md")


@dataclass(frozen=True)
class FeedbackPatternSyncResult:
    output_path: str
    changed: bool
    false_positive_count: int
    accepted_feedback_count: int
    total_count: int
    branch: Optional[str] = None
    pushed: bool = False
    merge_request_url: Optional[str] = None
    manual_merge_request_url: Optional[str] = None
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def default_feedback_patterns_path() -> Path:
    return Path(__file__).resolve().parent / "templates" / "references" / "false_positive_patterns.md"


def sync_feedback_patterns_from_db(db: ReviewDB, settings: Settings) -> FeedbackPatternSyncResult:
    patterns = _load_feedback_patterns(db, limit=max(1, int(settings.review_v2_feedback_pattern_sync_limit)))
    false_positive = [item for item in patterns if item["status"] == "resolved_model_false_positive"]
    accepted_feedback = [item for item in patterns if item["status"] != "resolved_model_false_positive"]
    base = {
        "output_path": str(TARGET_PATTERNS_REL),
        "false_positive_count": len(false_positive),
        "accepted_feedback_count": len(accepted_feedback),
        "total_count": len(patterns),
    }
    if not patterns:
        return FeedbackPatternSyncResult(changed=False, message="no resolved feedback patterns", **base)
    content = render_feedback_patterns(false_positive=false_positive, accepted_feedback=accepted_feedback)
    return create_feedback_patterns_merge_request(content, settings, **base)


def create_feedback_patterns_merge_request(
    content: str,
    settings: Settings,
    *,
    output_path: str,
    false_positive_count: int,
    accepted_feedback_count: int,
    total_count: int,
) -> FeedbackPatternSyncResult:
    repo_root = source_repo_root()
    target_branch = settings.review_v2_feedback_pattern_sync_target_branch or "init"
    work_repo = clone_isolated_repo(repo_root, settings=settings, target_branch=target_branch)
    try:
        configure_git_user(repo_root, work_repo)
        patterns_file = work_repo / TARGET_PATTERNS_REL
        existing = patterns_file.read_text(encoding="utf-8") if patterns_file.exists() else ""
        if existing == content:
            return FeedbackPatternSyncResult(
                output_path=output_path,
                changed=False,
                false_positive_count=false_positive_count,
                accepted_feedback_count=accepted_feedback_count,
                total_count=total_count,
                message="pattern reference already up to date in target branch",
            )
        patterns_file.parent.mkdir(parents=True, exist_ok=True)
        patterns_file.write_text(content, encoding="utf-8")
        ensure_review_reference(work_repo)
        branch = f"mr/feedback-pattern-sync-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        git(work_repo, "checkout", "-B", branch)
        git(work_repo, "add", str(TARGET_PATTERNS_REL), str(TARGET_REVIEW_REFERENCE_REL))
        git(work_repo, "commit", "-m", f"Sync CR v2 feedback patterns {datetime.now().strftime('%Y-%m-%d')}")
        git(work_repo, "push", "-u", "origin", branch)
        manual_url = manual_merge_request_url(settings, branch=branch, target_branch=target_branch)
        try:
            mr_url = create_merge_request(settings, branch=branch, target_branch=target_branch, manual_url=manual_url)
            message = "merge request created" if mr_url else "sync branch pushed; merge request requires manual creation"
        except RuntimeError as exc:
            mr_url = None
            message = f"sync branch pushed; merge request creation failed: {exc}"
        return FeedbackPatternSyncResult(
            output_path=output_path,
            changed=True,
            false_positive_count=false_positive_count,
            accepted_feedback_count=accepted_feedback_count,
            total_count=total_count,
            branch=branch,
            pushed=True,
            merge_request_url=mr_url,
            manual_merge_request_url=manual_url,
            message=message,
        )
    finally:
        if os.environ.get("CR_AGENT_KEEP_SYNC_WORKTREE") != "1":
            shutil.rmtree(work_repo, ignore_errors=True)


def source_repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def clone_isolated_repo(repo_root: Path, *, settings: Settings, target_branch: str) -> Path:
    work_root = settings.review_v2_feedback_pattern_sync_work_root or (settings.base_dir / "sync_worktrees")
    work_root.mkdir(parents=True, exist_ok=True)
    target = Path(mkdtemp(prefix=f"feedback-pattern-sync-{datetime.now().strftime('%Y%m%d-%H%M%S')}-", dir=str(work_root)))
    origin = git(repo_root, "remote", "get-url", "origin")
    completed = subprocess.run(
        ["git", "clone", "--branch", target_branch, origin, str(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr or completed.stdout)
    return target


def ensure_review_reference(repo_root: Path) -> None:
    review_reference_file = repo_root / TARGET_REVIEW_REFERENCE_REL
    review_reference_text = review_reference_file.read_text(encoding="utf-8")
    if "false_positive_patterns.md" not in review_reference_text:
        review_reference_file.write_text(
            review_reference_text.rstrip() + "\n- 模型反馈归档见 `references/false_positive_patterns.md`\n",
            encoding="utf-8",
        )


def manual_merge_request_url(settings: Settings, *, branch: str, target_branch: str) -> str:
    base_url = settings.gitlab_base_url.rstrip("/")
    if "github.com" in base_url:
        return (
            "https://github.com/comain/code-review-agent/compare/"
            f"{urllib.parse.quote(target_branch, safe='')}..."
            f"{urllib.parse.quote(branch, safe='')}?expand=1"
        )
    return (
        f"{base_url}/comain/code-review-agent/merge_requests/new"
        f"?merge_request[source_branch]={urllib.parse.quote(branch, safe='')}"
        f"&merge_request[target_branch]={urllib.parse.quote(target_branch, safe='')}"
    )


def create_merge_request(
    settings: Settings, *, branch: str, target_branch: str, manual_url: Optional[str] = None
) -> Optional[str]:
    manual_url = manual_url or manual_merge_request_url(settings, branch=branch, target_branch=target_branch)
    tokens = resolve_gitlab_tokens(settings)
    if not tokens:
        return None
    project = urllib.parse.quote_plus("comain/code-review-agent")
    url = f"{settings.gitlab_base_url.rstrip('/')}/api/v4/projects/{project}/merge_requests"
    payload = urllib.parse.urlencode(
        {
            "source_branch": branch,
            "target_branch": target_branch,
            "title": f"chore: sync CR v2 feedback patterns {datetime.now().strftime('%Y-%m-%d')}",
        }
    ).encode("utf-8")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    last_error = None
    for token in tokens:
        request = urllib.request.Request(
            url,
            data=payload,
            headers={"PRIVATE-TOKEN": token, "Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with opener.open(request, timeout=20) as response:
                data = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore")
            last_error = f"HTTP {exc.code} {exc.reason} {body}"
            if exc.code == 401:
                continue
            raise RuntimeError(f"GitLab MR creation failed: {last_error}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"GitLab MR creation failed: {exc}") from exc
        try:
            payload_json = json.loads(data)
        except Exception:
            return None
        return payload_json.get("web_url")
    if last_error:
        raise RuntimeError(f"GitLab MR creation failed: {last_error}")
    return None


def resolve_gitlab_tokens(settings: Settings) -> list[str]:
    token = settings.gitlab_api_token or os.environ.get("GITLAB_TOKEN") or os.environ.get("CR_AGENT_GITLAB_API_TOKEN")
    if token:
        return [token]
    return []


def configure_git_user(source_repo_root: Path, repo_root: Path) -> None:
    name = git_optional(source_repo_root, "config", "--get", "user.name") or "codex"
    email = git_optional(source_repo_root, "config", "--get", "user.email") or "codex@local"
    git(repo_root, "config", "user.name", name)
    git(repo_root, "config", "user.email", email)


def git(repo_root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr or completed.stdout)
    return completed.stdout.strip()


def git_optional(repo_root: Path, *args: str) -> Optional[str]:
    completed = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return None
    value = completed.stdout.strip()
    return value or None


def _load_feedback_patterns(db: ReviewDB, *, limit: int) -> list[dict[str, Any]]:
    status_placeholders = ",".join("?" for _ in RESOLVED_PATTERN_STATUSES)
    query = f"""
        SELECT
            f.finding_id,
            f.task_id,
            f.file_path,
            f.line,
            f.severity,
            f.title,
            f.detail,
            f.suggestion,
            f.status,
            f.updated_at,
            t.app_name,
            t.branch,
            t.commit_id,
            rr.reviewer,
            rr.session_id AS review_session_id,
            e.actor AS feedback_actor,
            e.message AS feedback_message,
            e.created_at AS feedback_at
        FROM findings f
        JOIN cr_tasks t ON t.task_id = f.task_id
        LEFT JOIN reviewer_runs rr ON rr.id = f.reviewer_run_id
        LEFT JOIN (
            SELECT e1.*
            FROM finding_events e1
            JOIN (
                SELECT finding_id, MAX(id) AS id
                FROM finding_events
                WHERE event_type = 'finding_status_changed'
                GROUP BY finding_id
            ) latest ON latest.id = e1.id
        ) e ON e.finding_id = f.finding_id
        WHERE f.status IN ({status_placeholders})
        ORDER BY COALESCE(e.created_at, f.updated_at) DESC, f.finding_id ASC
        LIMIT ?
    """
    with db.connect() as conn:
        rows = list(conn.execute(query, (*RESOLVED_PATTERN_STATUSES, limit)))
    return [dict(row) for row in rows]


def render_feedback_patterns(
    *, false_positive: Iterable[dict[str, Any]], accepted_feedback: Iterable[dict[str, Any]]
) -> str:
    sections = [
        "# 模型反馈归档",
        "",
        "本文件由 CR v2 SQLite feedback pattern sync 自动更新，用于沉淀用户反馈后确认的误判范式、已采纳反馈范式与漏判范式。",
        "",
        "## 误判范式",
        "",
        _render_items(false_positive) or "暂无新增误判范式。",
        "",
        "## 已采纳反馈范式",
        "",
        _render_items(accepted_feedback) or "暂无新增已采纳反馈范式。",
        "",
        "## 漏判范式",
        "",
        "CR v2 当前只从 finding feedback events 自动沉淀已反馈 finding；漏判范式仍需单独事件来源后再同步。",
        "",
    ]
    return "\n".join(sections)


def _render_items(items: Iterable[dict[str, Any]]) -> str:
    blocks = []
    for index, item in enumerate(items, start=1):
        title = _compact(item.get("title"), fallback="未命名反馈")
        feedback_message = _compact(item.get("feedback_message"), fallback="N/A", max_chars=500)
        detail = _compact(item.get("detail"), fallback="N/A", max_chars=500)
        suggestion = _compact(item.get("suggestion"), fallback="N/A", max_chars=300)
        line = item.get("line")
        location = item.get("file_path") or "N/A"
        if line is not None:
            location = f"{location}:{line}"
        blocks.append(
            "\n".join(
                [
                    f"### {index}. {title}",
                    f"- 来源应用: {_compact(item.get('app_name'), fallback='N/A')}",
                    f"- 分支: {_compact(item.get('branch'), fallback='N/A')}",
                    f"- 文件: {location}",
                    f"- 原始级别: {_compact(item.get('severity'), fallback='N/A')}",
                    f"- 反馈结论: {_compact(item.get('status'), fallback='N/A')}",
                    f"- 反馈人/模型: {_compact(item.get('feedback_actor'), fallback='N/A')}",
                    f"- 反馈说明: {feedback_message}",
                    f"- 原始问题: {detail}",
                    f"- 原始建议: {suggestion}",
                    "",
                ]
            )
        )
    return "\n".join(blocks).rstrip()


def _compact(value: Optional[Any], *, fallback: str, max_chars: int = 160) -> str:
    text = str(value or "").strip()
    if not text:
        return fallback
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."

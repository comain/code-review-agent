from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from tempfile import mkdtemp
from typing import Optional


SOURCE_REPO_ROOT = Path(__file__).resolve().parent.parent
ISSUES_DIR = Path(os.environ.get("CR_AGENT_ISSUES_DIR", "/opt/app/issues"))
WORK_ROOT = Path(os.environ.get("CR_AGENT_SYNC_WORK_ROOT", str(SOURCE_REPO_ROOT / "runtime" / "sync_worktrees")))
TARGET_PATTERNS_REL = Path("src/cr_agent/review_v2/templates/references/false_positive_patterns.md")
TARGET_REVIEW_REFERENCE_REL = Path("src/cr_agent/review_v2/templates/references/review.md")


def load_patterns(pattern: str, fallback_title: str) -> OrderedDict[str, dict]:
    patterns: OrderedDict[str, dict] = OrderedDict()
    for path in sorted(ISSUES_DIR.glob(pattern)):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            key = item.get("pattern_summary") or item.get("title") or item.get("content") or fallback_title
            patterns.setdefault(key, item)
    return patterns


def load_all_false_positive_patterns() -> OrderedDict[str, dict]:
    return load_patterns("*.false-positive.jsonl", "未命名模式")


def load_all_accepted_finding_feedback_patterns() -> OrderedDict[str, dict]:
    return load_patterns("*.accepted-finding-feedback.jsonl", "未命名采纳模式")


def load_all_missed_issue_patterns() -> OrderedDict[str, dict]:
    return load_patterns("*.missed-issue.jsonl", "未命名漏判模式")


def has_today_pattern_updates() -> bool:
    today = datetime.now().strftime("%Y-%m-%d")
    return any(
        (ISSUES_DIR / f"{today}.{suffix}.jsonl").exists()
        for suffix in ("false-positive", "accepted-finding-feedback", "missed-issue")
    )


def update_patterns_file(
    repo_root: Path,
    false_positive_patterns: OrderedDict[str, dict],
    accepted_feedback_patterns: OrderedDict[str, dict],
    missed_issue_patterns: OrderedDict[str, dict],
) -> bool:
    patterns_file = repo_root / TARGET_PATTERNS_REL
    review_reference_file = repo_root / TARGET_REVIEW_REFERENCE_REL
    existing = (
        patterns_file.read_text(encoding="utf-8")
        if patterns_file.exists()
        else "# 模型反馈归档\n\n本文件由每日同步脚本自动更新，用于沉淀用户反馈后确认的误判范式、已采纳反馈范式与漏判范式。\n"
    )
    false_positive_blocks = []
    for title, item in false_positive_patterns.items():
        false_positive_blocks.append(
            "\n".join(
                [
                    f"## {title}",
                    f"- 来源应用: {item.get('app_name', 'N/A')}",
                    f"- 文件: {item.get('file', 'N/A')}",
                    f"- 说明: {item.get('pattern_summary') or title}",
                    "",
                ]
            )
        )
    missed_issue_blocks = []
    for title, item in missed_issue_patterns.items():
        missed_issue_blocks.append(
            "\n".join(
                [
                    f"## {title}",
                    f"- 来源应用: {item.get('app_name', 'N/A')}",
                    f"- 漏判描述: {item.get('content', 'N/A')}",
                    f"- 模型结论: {item.get('reply', 'N/A')}",
                    f"- 说明: {item.get('pattern_summary') or title}",
                    "",
                ]
            )
        )
    accepted_feedback_blocks = []
    for title, item in accepted_feedback_patterns.items():
        accepted_feedback_blocks.append(
            "\n".join(
                [
                    f"## {title}",
                    f"- 来源应用: {item.get('app_name', 'N/A')}",
                    f"- 文件: {item.get('file', 'N/A')}",
                    f"- 采纳动作: {item.get('action', 'N/A')}",
                    f"- 调整后级别: {item.get('severity', 'N/A')}",
                    f"- 说明: {item.get('pattern_summary') or title}",
                    "",
                ]
            )
        )
    if not false_positive_blocks and not accepted_feedback_blocks and not missed_issue_blocks:
        return False
    header = "# 模型反馈归档\n\n本文件由每日同步脚本自动更新，用于沉淀用户反馈后确认的误判范式、已采纳反馈范式与漏判范式。\n\n"
    sections = []
    sections.append("## 误判范式\n")
    sections.append("\n".join(false_positive_blocks) if false_positive_blocks else "暂无新增误判范式。\n")
    sections.append("\n## 已采纳反馈范式\n")
    sections.append("\n".join(accepted_feedback_blocks) if accepted_feedback_blocks else "暂无新增已采纳反馈范式。\n")
    sections.append("\n## 漏判范式\n")
    sections.append("\n".join(missed_issue_blocks) if missed_issue_blocks else "暂无新增漏判范式。\n")
    updated = header + "\n".join(sections).rstrip() + "\n"
    if updated == existing:
        return False
    patterns_file.parent.mkdir(parents=True, exist_ok=True)
    patterns_file.write_text(updated, encoding="utf-8")
    review_reference_text = review_reference_file.read_text(encoding="utf-8")
    if "false_positive_patterns.md" not in review_reference_text:
        review_reference_file.write_text(
            review_reference_text.rstrip() + "\n- 模型反馈归档见 `references/false_positive_patterns.md`\n",
            encoding="utf-8",
        )
    return True


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


def clone_isolated_repo() -> Path:
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    target = Path(mkdtemp(prefix=f"false-positive-sync-{datetime.now().strftime('%Y%m%d-%H%M%S')}-", dir=str(WORK_ROOT)))
    origin = git(SOURCE_REPO_ROOT, "remote", "get-url", "origin")
    branch = git(SOURCE_REPO_ROOT, "rev-parse", "--abbrev-ref", "HEAD")
    completed = subprocess.run(
        ["git", "clone", "--branch", branch, origin, str(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr or completed.stdout)
    return target


def create_merge_request(branch: str) -> None:
    manual_url = f"https://github.com/comain/code-review-agent/compare/init...{branch}?expand=1"
    print("Create PR manually:", manual_url)


def configure_git_user(repo_root: Path) -> None:
    name = git_optional(SOURCE_REPO_ROOT, "config", "--get", "user.name") or "codex"
    email = git_optional(SOURCE_REPO_ROOT, "config", "--get", "user.email") or "codex@local"
    git(repo_root, "config", "user.name", name)
    git(repo_root, "config", "user.email", email)


def main() -> None:
    if not has_today_pattern_updates():
        print("No false-positive, accepted-feedback, or missed-issue patterns found today.")
        return
    false_positive_patterns = load_all_false_positive_patterns()
    accepted_feedback_patterns = load_all_accepted_finding_feedback_patterns()
    missed_issue_patterns = load_all_missed_issue_patterns()

    work_repo = clone_isolated_repo()
    try:
        configure_git_user(work_repo)
        changed = update_patterns_file(work_repo, false_positive_patterns, accepted_feedback_patterns, missed_issue_patterns)
        if not changed:
            print("No pattern file changes.")
            return

        branch = f"mr/false-positive-sync-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        git(work_repo, "checkout", "-B", branch)
        git(work_repo, "add", str(work_repo / TARGET_PATTERNS_REL), str(work_repo / TARGET_REVIEW_REFERENCE_REL))
        git(work_repo, "commit", "-m", f"Sync issue feedback patterns {datetime.now().strftime('%Y-%m-%d')}")
        git(work_repo, "push", "-u", "origin", branch)
        create_merge_request(branch)
        print(f"work_repo={work_repo}")
    finally:
        if os.environ.get("CR_AGENT_KEEP_SYNC_WORKTREE") != "1":
            shutil.rmtree(work_repo, ignore_errors=True)


if __name__ == "__main__":
    main()

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

from cr_agent.config import Settings
from cr_agent.core.git_identity import (
    git_env_with_identity,
    git_ssh_command_for_key,
    git_url_for_access_token,
    has_git_access_token,
)

logger = logging.getLogger(__name__)


class GitClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._repo_locks: Dict[str, threading.RLock] = {}
        self._repo_locks_guard = threading.Lock()
        self._git_access_token = settings.git_access_token.strip()
        self._git_ssh_command = None if self._git_access_token else git_ssh_command_for_key(settings.git_ssh_key_path)

        parsed_gitlab_url = urlparse(settings.gitlab_base_url)
        token_host = parsed_gitlab_url.hostname or "github.com"

        self._git_env = git_env_with_identity(
            ssh_key_path=settings.git_ssh_key_path,
            access_token=self._git_access_token,
            token_host=token_host,
        )

    def prepare_repo(
        self,
        repo_url: str,
        branch: str,
        commit_id: Optional[str],
        task_id: Optional[str] = None,
        is_cancelled: Optional[Callable[[str], bool]] = None,
    ) -> Path:
        self.validate_repo_url(repo_url)
        self.validate_branch(branch)
        if commit_id:
            self.validate_refish(commit_id)
        repo_name = self._repo_name(repo_url)
        repo_path = self.settings.repo_cache_dir / repo_name
        with self.repo_lock(repo_url):
            logger.info(
                "repo=%s prepare start branch=%s commit=%s path=%s",
                repo_name,
                branch,
                commit_id or "N/A",
                repo_path,
            )
            if not repo_path.exists():
                self._run(
                    [
                        "git",
                        "clone",
                        "--branch",
                        branch,
                        "--depth",
                        str(self.settings.git_clone_depth),
                        "--",
                        self._clone_url(repo_url),
                        str(repo_path),
                    ],
                    task_id=task_id,
                    is_cancelled=is_cancelled,
                )
                if has_git_access_token(self._git_access_token):
                    self._configure_repo_remote_url(repo_path, repo_url, task_id=task_id, is_cancelled=is_cancelled)
                self._configure_repo_ssh_command(repo_path, task_id=task_id, is_cancelled=is_cancelled)
            else:
                if has_git_access_token(self._git_access_token):
                    self._configure_repo_remote_url(repo_path, repo_url, task_id=task_id, is_cancelled=is_cancelled)
                self._configure_repo_ssh_command(repo_path, task_id=task_id, is_cancelled=is_cancelled)
                self._fetch_branch_ref(
                    repo_path,
                    branch,
                    task_id=task_id,
                    is_cancelled=is_cancelled,
                )

            ref = commit_id or f"origin/{branch}"
            self._run(["git", "-C", str(repo_path), "switch", "--detach", "--force", "--", ref], task_id=task_id, is_cancelled=is_cancelled)
            self._run(["git", "-C", str(repo_path), "clean", "-fd"], task_id=task_id, is_cancelled=is_cancelled)
            logger.info(
                "repo=%s prepare finished ref=%s",
                repo_name,
                ref,
            )
        return repo_path

    def prepare_fix_workspace(
        self,
        repo_url: str,
        branch: str,
        commit_id: Optional[str],
        workspace_dir: Path,
        fix_branch: str,
        task_id: Optional[str] = None,
        is_cancelled: Optional[Callable[[str], bool]] = None,
    ) -> Path:
        self.validate_repo_url(repo_url)
        self.validate_branch(branch)
        self.validate_branch(fix_branch)
        if commit_id:
            self.validate_refish(commit_id)
        repo_name = self._repo_name(repo_url)
        repo_path = workspace_dir / repo_name
        if workspace_dir.exists():
            shutil.rmtree(workspace_dir)
        workspace_dir.mkdir(parents=True, exist_ok=True)
        self._run(
            ["git", "clone", "--", self._clone_url(repo_url), str(repo_path)],
            task_id=task_id,
            is_cancelled=is_cancelled,
        )
        if has_git_access_token(self._git_access_token):
            self._configure_repo_remote_url(repo_path, repo_url, task_id=task_id, is_cancelled=is_cancelled)
        self._configure_repo_ssh_command(repo_path, task_id=task_id, is_cancelled=is_cancelled)
        ref = commit_id or f"origin/{branch}"
        self._fetch_branch_ref(
            repo_path,
            branch,
            task_id=task_id,
            is_cancelled=is_cancelled,
        )
        self._run(["git", "-C", str(repo_path), "switch", "--detach", "--force", "--", ref], task_id=task_id, is_cancelled=is_cancelled)
        self._run(["git", "-C", str(repo_path), "checkout", "-B", fix_branch], task_id=task_id, is_cancelled=is_cancelled)
        self._run(["git", "-C", str(repo_path), "clean", "-fd"], task_id=task_id, is_cancelled=is_cancelled)
        return repo_path

    def collect_review_context(
        self,
        repo_path: Path,
        task_id: Optional[str] = None,
        is_cancelled: Optional[Callable[[str], bool]] = None,
    ) -> Dict[str, Any]:
        self._refresh_base_refs(repo_path, task_id=task_id, is_cancelled=is_cancelled)
        diff_range = self._resolve_diff_range(repo_path)
        changed_files = self._run_output(
            ["git", "-C", str(repo_path), "diff", "--name-only", "--diff-filter=ACMR", diff_range],
            task_id=task_id,
            is_cancelled=is_cancelled,
        ).splitlines()
        changed_files = [item.strip() for item in changed_files if item.strip()]
        non_test_changed_files = [item for item in changed_files if not self._is_test_file(item)]
        diff_stat = self._run_output(
            ["git", "-C", str(repo_path), "diff", "--stat=160", diff_range],
            task_id=task_id,
            is_cancelled=is_cancelled,
        ).strip()
        commit_log = self._run_output(
            ["git", "-C", str(repo_path), "log", "--oneline", "--no-merges", diff_range],
            task_id=task_id,
            is_cancelled=is_cancelled,
        ).strip()
        logger.info(
            "repo=%s review context collected diff_range=%s changed_files=%s non_test_changed_files=%s",
            repo_path.name,
            diff_range,
            len(changed_files),
            len(non_test_changed_files),
        )
        return {
            "diff_range": diff_range,
            "changed_files": changed_files,
            "non_test_changed_files": non_test_changed_files,
            "diff_stat": diff_stat,
            "commit_log": commit_log,
        }

    def current_commit(
        self,
        repo_path: Path,
        task_id: Optional[str] = None,
        is_cancelled: Optional[Callable[[str], bool]] = None,
    ) -> str:
        commit = self._run_output(
            ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
            task_id=task_id,
            is_cancelled=is_cancelled,
        ).strip()
        if not commit:
            raise RuntimeError(f"failed to resolve HEAD commit for repo: {repo_path}")
        return commit

    def _resolve_diff_range(self, repo_path: Path) -> str:
        for base_ref in self._candidate_base_refs(repo_path):
            if not self._ref_exists(repo_path, base_ref):
                continue
            merge_base = self._run_output_optional(
                ["git", "-C", str(repo_path), "merge-base", "HEAD", base_ref]
            )
            if merge_base:
                return f"{merge_base}..HEAD"

        for fallback in ("HEAD~5", "HEAD~3", "HEAD~1"):
            if self._ref_exists(repo_path, fallback):
                return f"{fallback}..HEAD"
        return "HEAD"

    def _candidate_base_refs(self, repo_path: Path) -> List[str]:
        default_ref = "origin/master"
        git_master_path = repo_path / ".git_master"
        if not git_master_path.exists():
            return [default_ref]

        branch = git_master_path.read_text(encoding="utf-8").strip()
        if not branch:
            return [default_ref]
        try:
            self.validate_branch(branch.removeprefix("origin/"))
        except ValueError:
            logger.warning("repo=%s ignore unsafe .git_master ref=%s", repo_path.name, branch)
            return [default_ref]
        if branch.startswith("origin/"):
            return [branch]
        return [f"origin/{branch}"]

    def repo_lock(self, repo_url: str) -> threading.RLock:
        repo_name = self._repo_name(repo_url)
        with self._repo_locks_guard:
            lock = self._repo_locks.get(repo_name)
            if lock is None:
                lock = threading.RLock()
                self._repo_locks[repo_name] = lock
            return lock

    def _refresh_base_refs(
        self,
        repo_path: Path,
        task_id: Optional[str] = None,
        is_cancelled: Optional[Callable[[str], bool]] = None,
    ) -> None:
        if self._has_local_origin(repo_path):
            logger.info("repo=%s skip base ref refresh for local origin", repo_path.name)
            return
        for base_ref in self._candidate_base_refs(repo_path):
            if not base_ref.startswith("origin/"):
                continue
            remote_branch = base_ref.removeprefix("origin/")
            remote_ref = f"refs/heads/{remote_branch}:refs/remotes/{base_ref}"
            logger.info("repo=%s refresh base ref=%s", repo_path.name, base_ref)
            completed = self._run_completed(
                ["git", "-C", str(repo_path), "fetch", "origin", "--prune", "--", remote_ref],
                task_id=task_id,
                is_cancelled=is_cancelled,
            )
            if completed.returncode != 0:
                stderr = completed.stderr or ""
                if "couldn't find remote ref" in stderr:
                    logger.info("repo=%s skip missing base ref=%s", repo_path.name, base_ref)
                    continue
                raise RuntimeError(
                    f"git command failed: git -C {repo_path} fetch origin {remote_ref} --prune\n"
                    f"stdout={completed.stdout}\nstderr={completed.stderr}"
                )

    def _has_local_origin(self, repo_path: Path) -> bool:
        if not (repo_path / ".git").exists():
            return False
        origin = self._run_output_optional(["git", "-C", str(repo_path), "remote", "get-url", "origin"])
        if not origin:
            return False
        parsed = urlparse(origin)
        if parsed.scheme == "file":
            return True
        if parsed.scheme:
            return False
        if re.match(r"^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[^\\\s]+$", origin):
            return False
        return origin.startswith(("/", "./", "../")) or Path(origin).exists()

    def _configure_repo_ssh_command(
        self,
        repo_path: Path,
        task_id: Optional[str] = None,
        is_cancelled: Optional[Callable[[str], bool]] = None,
    ) -> None:
        if not self._git_ssh_command:
            return
        self._run(
            ["git", "-C", str(repo_path), "config", "core.sshCommand", self._git_ssh_command],
            task_id=task_id,
            is_cancelled=is_cancelled,
        )

    def _fetch_branch_ref(
        self,
        repo_path: Path,
        branch: str,
        task_id: Optional[str] = None,
        is_cancelled: Optional[Callable[[str], bool]] = None,
    ) -> None:
        self.validate_branch(branch)
        remote_ref = f"refs/heads/{branch}:refs/remotes/origin/{branch}"
        self._run(
            ["git", "-C", str(repo_path), "fetch", "origin", "--prune", "--", remote_ref],
            task_id=task_id,
            is_cancelled=is_cancelled,
        )

    @staticmethod
    def _is_test_file(path: str) -> bool:
        lowered = path.lower()
        return (
            "/src/test/" in lowered
            or "/src/main/test/" in lowered
            or lowered.endswith("test.java")
            or lowered.endswith("tests.java")
            or lowered.endswith("it.java")
        )

    def _ref_exists(self, repo_path: Path, ref: str) -> bool:
        completed = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "--verify", ref],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=self._git_env,
        )
        return completed.returncode == 0

    def _run_output(
        self,
        cmd: List[str],
        task_id: Optional[str] = None,
        is_cancelled: Optional[Callable[[str], bool]] = None,
    ) -> str:
        completed = self._run_completed(cmd, task_id=task_id, is_cancelled=is_cancelled)
        if completed.returncode != 0:
            raise RuntimeError(
                f"git command failed: {' '.join(cmd)}\nstdout={completed.stdout}\nstderr={completed.stderr}"
            )
        return completed.stdout

    def _run_output_optional(
        self,
        cmd: List[str],
        task_id: Optional[str] = None,
        is_cancelled: Optional[Callable[[str], bool]] = None,
    ) -> Optional[str]:
        completed = self._run_completed(cmd, task_id=task_id, is_cancelled=is_cancelled)
        if completed.returncode != 0:
            return None
        return completed.stdout.strip() or None

    @staticmethod
    def _repo_name(repo_url: str) -> str:
        return repo_url.rstrip("/").split("/")[-1].removesuffix(".git")

    def commit_all_and_push(self, repo_path: Path, branch: str, message: str) -> bool:
        self.validate_branch(branch)
        self._run(["git", "-C", str(repo_path), "config", "user.name", "cr-agent-bot"])
        self._run(["git", "-C", str(repo_path), "config", "user.email", "cr-agent@local"])
        self._configure_repo_access_token_remote(repo_path)
        self._run(["git", "-C", str(repo_path), "add", "-A"])
        status = self._run_output_optional(["git", "-C", str(repo_path), "status", "--porcelain"])
        if not status:
            return False
        self._run(["git", "-C", str(repo_path), "commit", "-m", message])
        self._run(["git", "-C", str(repo_path), "push", "-u", "origin", branch])
        return True

    def _clone_url(self, git_url: str) -> str:
        self.validate_repo_url(git_url)
        if not has_git_access_token(self._git_access_token):
            return git_url
        return git_url_for_access_token(git_url)

    @staticmethod
    def validate_repo_url(repo_url: str) -> None:
        if not repo_url or repo_url.startswith("-"):
            raise ValueError("unsafe git repo_url")
        parsed = urlparse(repo_url)
        if parsed.scheme:
            if parsed.scheme not in {"https", "ssh"}:
                raise ValueError("unsupported git repo_url scheme")
            if not parsed.hostname:
                raise ValueError("git repo_url host is required")
            return
        if re.match(r"^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[^\\\s]+$", repo_url):
            return
        raise ValueError("unsupported git repo_url format")

    @staticmethod
    def validate_branch(branch: str) -> None:
        GitClient.validate_refish(branch)
        if branch.startswith("origin/"):
            branch = branch.removeprefix("origin/")
        if branch.startswith("/") or branch.endswith("/") or branch.endswith(".") or branch.endswith(".lock"):
            raise ValueError("unsafe git branch")

    @staticmethod
    def validate_refish(ref: str) -> None:
        if not ref or ref.startswith("-"):
            raise ValueError("unsafe git ref")
        if any(ord(ch) < 32 or ch.isspace() for ch in ref):
            raise ValueError("unsafe git ref")
        forbidden = ("..", "~", "^", ":", "?", "*", "[", "\\", "@{", "//")
        if any(item in ref for item in forbidden):
            raise ValueError("unsafe git ref")

    def _configure_repo_remote_url(
        self,
        repo_path: Path,
        repo_url: str,
        task_id: Optional[str] = None,
        is_cancelled: Optional[Callable[[str], bool]] = None,
    ) -> None:
        target_url = self._clone_url(repo_url)
        current = self._run_output_optional(
            ["git", "-C", str(repo_path), "remote", "get-url", "origin"],
            task_id=task_id,
            is_cancelled=is_cancelled,
        )
        if current:
            current = current.strip()
        if current != target_url:
            logger.info("repo=%s remote url mismatch current=%s target=%s", repo_path.name, current, target_url)
            self._run(
                ["git", "-C", str(repo_path), "remote", "set-url", "origin", target_url],
                task_id=task_id,
                is_cancelled=is_cancelled,
            )

    def _configure_repo_access_token_remote(self, repo_path: Path) -> None:
        if not has_git_access_token(self._git_access_token):
            return
        current = self._run_output_optional(["git", "-C", str(repo_path), "remote", "get-url", "origin"])
        if current:
            current = current.strip()
            rewritten = git_url_for_access_token(current)
            if rewritten and rewritten != current:
                self._run(["git", "-C", str(repo_path), "remote", "set-url", "origin", rewritten])

    def _run(
        self,
        cmd: List[str],
        task_id: Optional[str] = None,
        is_cancelled: Optional[Callable[[str], bool]] = None,
    ) -> None:
        completed = self._run_completed(cmd, task_id=task_id, is_cancelled=is_cancelled)
        if completed.returncode != 0:
            raise RuntimeError(
                f"git command failed: {' '.join(cmd)}\nstdout={completed.stdout}\nstderr={completed.stderr}"
            )

    def _run_completed(
        self,
        cmd: List[str],
        task_id: Optional[str] = None,
        is_cancelled: Optional[Callable[[str], bool]] = None,
    ) -> subprocess.CompletedProcess[str]:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=self._git_env,
        )
        while True:
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                return subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)
            if task_id and is_cancelled and is_cancelled(task_id):
                process.terminate()
                try:
                    stdout, stderr = process.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    stdout, stderr = process.communicate()
                raise RuntimeError("task cancelled by admin")
            time.sleep(0.2)

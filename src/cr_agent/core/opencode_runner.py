from __future__ import annotations

import json
import logging
import re
import shlex
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from textwrap import dedent
from typing import Any, Callable, Dict, Optional

from cr_agent.config import Settings
from cr_agent.core.usage import UsageStore
from cr_agent.models import AnalysisResult, FindingSeverity
from pydantic import ValidationError

logger = logging.getLogger(__name__)


class UnrepairableOpencodeOutputError(RuntimeError):
    """Raised when opencode did not produce a repairable model answer."""


class OpencodeRunner:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.usage = UsageStore(settings.usage_dir)

    def analyze(
        self,
        task_id: str,
        repo_path: Path,
        branch: str,
        commit_id: Optional[str],
        metadata: Dict[str, Any],
        is_cancelled: Optional[Callable[[str], bool]] = None,
    ) -> AnalysisResult:
        prompt_file = self.settings.task_dir / f"{task_id}.prompt.md"
        prompt_file.write_text(
            self._build_prompt(repo_path=repo_path, branch=branch, commit_id=commit_id, metadata=metadata),
            encoding="utf-8",
        )
        logger.info(
            "task=%s opencode prompt prepared prompt_file=%s repo_path=%s branch=%s commit=%s",
            task_id,
            prompt_file,
            repo_path,
            branch,
            commit_id or "N/A",
        )

        last_error: Optional[str] = None
        suspicious_empty_retry_used = False
        total_attempts = self.settings.llm_retry_times + 1
        attempt = 0
        while attempt < total_attempts:
            logger.info(
                "task=%s opencode analyze attempt=%s/%s start",
                task_id,
                attempt + 1,
                total_attempts,
            )
            completed = self._run_prompt(
                task_id=task_id,
                prompt_file=prompt_file,
                repo_path=repo_path,
                is_cancelled=is_cancelled,
            )
            self._record_usage(task_id=task_id, category="analysis", completed=completed)
            logger.info(
                "task=%s opencode analyze attempt=%s/%s finished returncode=%s stdout_chars=%s stderr_chars=%s",
                task_id,
                attempt + 1,
                total_attempts,
                completed.returncode,
                len(completed.stdout or ""),
                len(completed.stderr or ""),
            )
            if completed.returncode == 0:
                result, repair_attempt_used = self._parse_with_repair_loop(
                    task_id=task_id,
                    repo_path=repo_path,
                    raw_output=completed.stdout,
                    is_cancelled=is_cancelled,
                )
                if (
                    not suspicious_empty_retry_used
                    and self._is_suspicious_empty_repair_result(
                        raw_output=completed.stdout or "",
                        repair_attempt_used=repair_attempt_used,
                        result=result,
                    )
                ):
                    suspicious_empty_retry_used = True
                    total_attempts += 1
                    logger.warning(
                        "task=%s suspicious empty result after repair detected, triggering one extra analyze retry",
                        task_id,
                    )
                    attempt += 1
                    continue
                return result
            last_error = f"returncode={completed.returncode}, stderr={completed.stderr}, stdout={completed.stdout}"
            logger.warning(
                "task=%s opencode analyze attempt=%s/%s failed stderr_preview=%r stdout_preview=%r",
                task_id,
                attempt + 1,
                total_attempts,
                (completed.stderr or "")[:300],
                (completed.stdout or "")[:300],
            )
            attempt += 1

        raise RuntimeError(f"opencode analyze failed after retries: {last_error}")

    def review_finding_feedback(
        self,
        *,
        task_id: str,
        repo_path: Path,
        commit_id: Optional[str],
        finding: Dict[str, Any],
        code_context: str,
        conversation: list[dict[str, Any]],
        is_cancelled: Optional[Callable[[str], bool]] = None,
    ) -> Dict[str, Any]:
        prompt_file = self.settings.task_dir / f"{task_id}.feedback.{finding.get('index', 0)}.prompt.md"
        prompt_file.write_text(
            self._build_feedback_prompt(
                commit_id=commit_id,
                finding=finding,
                code_context=code_context,
                conversation=conversation,
            ),
            encoding="utf-8",
        )
        completed = self._run_prompt(
            task_id=task_id,
            prompt_file=prompt_file,
            repo_path=repo_path,
            is_cancelled=is_cancelled,
        )
        self._record_usage(task_id=task_id, category="finding_feedback", completed=completed)
        if completed.returncode != 0:
            raise RuntimeError(
                "opencode feedback review failed: "
                f"returncode={completed.returncode}, stderr={completed.stderr}, stdout={completed.stdout}"
            )
        data = self._parse_feedback_output(completed.stdout)
        action = data.get("action")
        if action not in {"keep", "downgrade", "resolve_false_positive"}:
            raise RuntimeError(f"invalid feedback action: {action}")
        severity = data.get("severity")
        if action == "downgrade" and severity not in {item.value for item in FindingSeverity}:
            raise RuntimeError(f"invalid downgrade severity: {severity}")
        return data

    def review_missed_issue_feedback(
        self,
        *,
        task_id: str,
        repo_path: Path,
        branch: str,
        commit_id: Optional[str],
        existing_summary: str,
        missed_issue: str,
        is_cancelled: Optional[Callable[[str], bool]] = None,
    ) -> Dict[str, Any]:
        prompt_file = self.settings.task_dir / f"{task_id}.general-feedback.prompt.md"
        prompt_file.write_text(
            self._build_missed_issue_feedback_prompt(
                branch=branch,
                commit_id=commit_id,
                existing_summary=existing_summary,
                missed_issue=missed_issue,
            ),
            encoding="utf-8",
        )
        completed = self._run_prompt(
            task_id=task_id,
            prompt_file=prompt_file,
            repo_path=repo_path,
            is_cancelled=is_cancelled,
        )
        self._record_usage(task_id=task_id, category="missed_issue_feedback", completed=completed)
        if completed.returncode != 0:
            raise RuntimeError(
                "opencode missed-issue review failed: "
                f"returncode={completed.returncode}, stderr={completed.stderr}, stdout={completed.stdout}"
            )
        data = self._parse_feedback_output(completed.stdout)
        if not isinstance(data.get("confirmed"), bool):
            raise RuntimeError("invalid missed-issue feedback result: confirmed must be boolean")
        if not isinstance(data.get("reply"), str) or not data.get("reply"):
            raise RuntimeError("invalid missed-issue feedback result: reply is required")
        return data

    def review_fix_conversation(
        self,
        *,
        task_id: str,
        repo_url: str,
        app_name: str,
        branch: str,
        commit_id: Optional[str],
        stage: str,
        selected_findings: list[dict[str, Any]],
        scope_summary: Optional[str],
        plan_summary: Optional[str],
        conversation: list[dict[str, Any]],
    ) -> Dict[str, Any]:
        prompt_file = self.settings.task_dir / f"{task_id}.fix-conversation.prompt.md"
        prompt_file.write_text(
            self._build_fix_conversation_prompt(
                repo_url=repo_url,
                app_name=app_name,
                branch=branch,
                commit_id=commit_id,
                stage=stage,
                selected_findings=selected_findings,
                scope_summary=scope_summary,
                plan_summary=plan_summary,
                conversation=conversation,
            ),
            encoding="utf-8",
        )
        completed = self._run_prompt(task_id=task_id, prompt_file=prompt_file, repo_path=Path("."), is_cancelled=None)
        self._record_usage(task_id=task_id, category="fix_conversation", completed=completed)
        if completed.returncode != 0:
            raise RuntimeError(
                "opencode fix conversation failed: "
                f"returncode={completed.returncode}, stderr={completed.stderr}, stdout={completed.stdout}"
            )
        data = self._parse_feedback_output(completed.stdout)
        if data.get("next_stage") not in {"scope_confirmation", "plan_confirmation", "fixing"}:
            raise RuntimeError(f"invalid fix next_stage: {data.get('next_stage')}")
        if not data.get("target_repo_url"):
            data["target_repo_url"] = repo_url
        if not data.get("target_branch"):
            data["target_branch"] = branch
        return data

    def apply_fix_session(
        self,
        *,
        task_id: str,
        repo_path: Path,
        repo_url: str,
        branch: str,
        commit_id: Optional[str],
        selected_findings: list[dict[str, Any]],
        scope_summary: Optional[str],
        plan_summary: Optional[str],
        conversation: list[dict[str, Any]],
        attempt: int,
        fix_skill_path: Path,
    ) -> str:
        prompt_file = self.settings.task_dir / f"{task_id}.fix-apply.{attempt}.prompt.md"
        prompt_file.write_text(
            self._build_fix_apply_prompt(
                repo_path=repo_path,
                repo_url=repo_url,
                branch=branch,
                commit_id=commit_id,
                selected_findings=selected_findings,
                scope_summary=scope_summary,
                plan_summary=plan_summary,
                conversation=conversation,
                fix_skill_path=fix_skill_path,
            ),
            encoding="utf-8",
        )
        completed = self._run_prompt(task_id=task_id, prompt_file=prompt_file, repo_path=repo_path, is_cancelled=None)
        self._record_usage(task_id=task_id, category="fix_apply", completed=completed)
        if completed.returncode != 0:
            raise RuntimeError(
                "opencode fix apply failed: "
                f"returncode={completed.returncode}, stderr={completed.stderr}, stdout={completed.stdout}"
            )
        return self._parse_fix_apply_output(completed.stdout)

    def review_fix_result(
        self,
        *,
        task_id: str,
        repo_path: Path,
        repo_url: str,
        branch: str,
        commit_id: Optional[str],
        selected_findings: list[dict[str, Any]],
        summary: str,
        scope_summary: Optional[str],
        plan_summary: Optional[str],
    ) -> Dict[str, Any]:
        prompt_file = self.settings.task_dir / f"{task_id}.fix-review.prompt.md"
        prompt_file.write_text(
            self._build_fix_review_prompt(
                repo_path=repo_path,
                repo_url=repo_url,
                branch=branch,
                commit_id=commit_id,
                selected_findings=selected_findings,
                summary=summary,
                scope_summary=scope_summary,
                plan_summary=plan_summary,
            ),
            encoding="utf-8",
        )
        completed = self._run_prompt(task_id=task_id, prompt_file=prompt_file, repo_path=repo_path, is_cancelled=None)
        self._record_usage(task_id=task_id, category="fix_review", completed=completed)
        if completed.returncode != 0:
            raise RuntimeError(
                "opencode fix review failed: "
                f"returncode={completed.returncode}, stderr={completed.stderr}, stdout={completed.stdout}"
            )
        data = self._parse_feedback_output(completed.stdout)
        if not isinstance(data.get("passed"), bool):
            raise RuntimeError("invalid fix review result: passed must be boolean")
        return data

    def _parse_feedback_output(self, stdout: str) -> Dict[str, Any]:
        text = stdout.strip()
        if not text:
            raise RuntimeError("empty opencode feedback output")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = self._parse_event_stream(text)
        if not isinstance(data, dict):
            raise RuntimeError(f"feedback output is not a json object: {data}")
        return data

    def _parse_fix_apply_output(self, stdout: str) -> str:
        text = stdout.strip()
        if not text:
            raise RuntimeError("empty opencode fix apply output")
        try:
            data = self._parse_feedback_output(text)
        except RuntimeError:
            logger.warning("opencode fix apply output is not json, fallback to plain text summary")
            return text
        summary = data.get("summary") if isinstance(data, dict) else None
        if isinstance(summary, str) and summary.strip():
            return summary.strip()
        logger.warning("opencode fix apply output json has no summary, fallback to raw text")
        return text

    def _parse_with_repair_loop(
        self,
        task_id: str,
        repo_path: Path,
        raw_output: str,
        is_cancelled: Optional[Callable[[str], bool]] = None,
    ) -> tuple[AnalysisResult, int]:
        last_error: Optional[Exception] = None
        candidate = raw_output
        session_ids = self._extract_session_ids(raw_output)
        for attempt in range(self.settings.llm_format_retry_times + 1):
            try:
                result = self._parse_output(candidate)
                self._attach_session_ids(result, session_ids)
                logger.info(
                    "task=%s opencode output parsed repair_attempt=%s findings=%s score=%s pass_check=%s",
                    task_id,
                    attempt,
                    len(result.findings),
                    result.score,
                    result.pass_check,
                )
                return result, attempt
            except UnrepairableOpencodeOutputError:
                raise
            except (RuntimeError, ValidationError, ValueError) as exc:
                last_error = exc
                logger.warning(
                    "task=%s opencode output parse failed repair_attempt=%s/%s error=%s",
                    task_id,
                    attempt,
                    self.settings.llm_format_retry_times,
                    exc,
                )
                if attempt >= self.settings.llm_format_retry_times:
                    break
                candidate = self._repair_output_with_model(
                    task_id=task_id,
                    repo_path=repo_path,
                    broken_output=candidate,
                    error_message=str(exc),
                    attempt=attempt + 1,
                    is_cancelled=is_cancelled,
                )
                for session_id in self._extract_session_ids(candidate):
                    self._append_unique_session_id(session_ids, session_id)

        raise RuntimeError(f"opencode output validation failed after repair retries: {last_error}")

    @staticmethod
    def _attach_session_ids(result: AnalysisResult, session_ids: list[str]) -> None:
        if not session_ids:
            return
        raw_output = dict(result.raw_output or {})
        raw_output["_opencode_session_ids"] = session_ids
        result.raw_output = raw_output

    @staticmethod
    def _is_suspicious_empty_repair_result(
        *,
        raw_output: str,
        repair_attempt_used: int,
        result: AnalysisResult,
    ) -> bool:
        if repair_attempt_used <= 0 or result.findings:
            return False
        text = (raw_output or "").strip()
        if not text:
            return False
        lowered = text.lower()
        suspicious_markers = (
            "i'll analyze",
            "let me start",
            "based on my",
            "i will",
            "i'll review",
            "我将先",
            "我先",
            "我会按",
            "让我先",
            "下面我来",
            "先并行收集",
        )
        return any(marker in lowered for marker in suspicious_markers)

    def _repair_output_with_model(
        self,
        task_id: str,
        repo_path: Path,
        broken_output: str,
        error_message: str,
        attempt: int,
        is_cancelled: Optional[Callable[[str], bool]] = None,
    ) -> str:
        prompt_file = self.settings.task_dir / f"{task_id}.repair.{attempt}.prompt.md"
        prompt_file.write_text(
            self._build_repair_prompt(broken_output=broken_output, error_message=error_message),
            encoding="utf-8",
        )
        logger.info(
            "task=%s opencode repair attempt=%s/%s start prompt_file=%s error=%s",
            task_id,
            attempt,
            self.settings.llm_format_retry_times,
            prompt_file,
            error_message,
        )
        completed = self._run_prompt(
            task_id=task_id,
            prompt_file=prompt_file,
            repo_path=repo_path,
            is_cancelled=is_cancelled,
        )
        self._record_usage(task_id=task_id, category="analysis_repair", completed=completed)
        if completed.returncode != 0:
            raise RuntimeError(
                "opencode repair failed: "
                f"returncode={completed.returncode}, stderr={completed.stderr}, stdout={completed.stdout}"
            )
        logger.info(
            "task=%s opencode repair attempt=%s/%s finished stdout_chars=%s stderr_chars=%s",
            task_id,
            attempt,
            self.settings.llm_format_retry_times,
            len(completed.stdout or ""),
            len(completed.stderr or ""),
        )
        return completed.stdout

    def _run_prompt(
        self,
        task_id: str,
        prompt_file: Path,
        repo_path: Path,
        is_cancelled: Optional[Callable[[str], bool]] = None,
    ) -> subprocess.CompletedProcess[str]:
        self._install_project_opencode_config(repo_path)
        command = self.settings.opencode_command_template.format(
            opencode_bin=self.settings.opencode_bin,
            repo_path=shlex.quote(str(repo_path)),
            prompt_file=shlex.quote(str(prompt_file)),
            skill_path=shlex.quote(self._review_guideline_label()),
        )
        process = subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        started_at = time.time()
        while True:
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
            if is_cancelled and is_cancelled(task_id):
                process.terminate()
                try:
                    stdout, stderr = process.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    stdout, stderr = process.communicate()
                raise RuntimeError("task cancelled by admin")
            if time.time() - started_at > self.settings.task_timeout_seconds:
                process.terminate()
                try:
                    stdout, stderr = process.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    stdout, stderr = process.communicate()
                raise RuntimeError("opencode prompt timeout")
            time.sleep(0.2)

    @staticmethod
    def _install_project_opencode_config(repo_path: Path) -> None:
        source = Path("opencode.json")
        if not source.is_file():
            return

        target = repo_path / "opencode.json"
        if source.resolve() == target.resolve() or target.exists():
            return

        shutil.copyfile(source, target)
        logger.info("installed project opencode config path=%s", target)

    def _record_usage(self, *, task_id: str, category: str, completed: subprocess.CompletedProcess[str]) -> None:
        try:
            usage = self._extract_usage_metrics(completed.stdout or "")
            self.usage.append(
                task_id=task_id,
                category=category,
                prompt_tokens=usage["prompt_tokens"],
                completion_tokens=usage["completion_tokens"],
                thinking_tokens=usage["thinking_tokens"],
                total_tokens=usage["total_tokens"],
                returncode=completed.returncode,
            )
        except Exception:  # noqa: BLE001
            logger.exception("task=%s usage record failed category=%s", task_id, category)

    def _build_prompt(
        self,
        repo_path: Path,
        branch: str,
        commit_id: Optional[str],
        metadata: Dict[str, Any],
    ) -> str:
        review_context = metadata.get("review_context") if isinstance(metadata, dict) else None
        non_test_changed_files = []
        diff_range = "N/A"
        diff_stat = "N/A"
        commit_log = "N/A"
        if isinstance(review_context, dict):
            non_test_changed_files = review_context.get("non_test_changed_files") or []
            diff_range = review_context.get("diff_range") or "N/A"
            diff_stat = review_context.get("diff_stat") or "N/A"
            commit_log = review_context.get("commit_log") or "N/A"

        return dedent(
            f"""
            你要对本地 git 工程做静态分析，并严格按 JSON 输出结果。

            仓库路径: {repo_path}
            分支: {branch}
            commit: {commit_id or "N/A"}
            review guideline: {self._review_guideline_label()}
            diff 范围: {diff_range}
            本次变更的非测试文件:
            {json.dumps(non_test_changed_files, ensure_ascii=False, indent=2)}
            diff 摘要:
            {diff_stat}
            提交摘要:
            {commit_log}
            附加上下文: {json.dumps(metadata, ensure_ascii=False)}

            要求:
            1. 按 CR v2 review guideline 审查本次变更直接引入或暴露的问题，优先只看上面的非测试变更文件。
            2. 忽略测试类、测试目录、样例代码和仅用于验证的辅助脚本，不要把它们作为 findings。
            3. 未变更文件只有在被本次变更直接调用、修改了使用方式、或者明显被本次变更放大风险时才能报告，并在 detail 中说明关联关系。
            4. 重点检查并发、超时、重试、幂等、异常处理、回调、防重复执行与配置风险。
            5. summary、title、detail、suggestion 必须使用中文；JSON key 保持英文。
            6. 仅输出一个 JSON 对象，不要输出 Markdown。

            JSON schema:
            {{
              "summary": "string",
              "pass_check": true,
              "score": 0,
              "findings": [
                {{
                  "file": "relative/path",
                  "line": 1,
                  "severity": "fatal|high|medium|low|info",
                  "title": "string",
                  "detail": "string",
                  "suggestion": "string"
                }}
              ]
            }}
            """
        ).strip()

    @staticmethod
    def _review_guideline_label() -> str:
        return "cr_agent.review_v2.templates/references/review.md"

    @staticmethod
    def _build_feedback_prompt(
        *,
        commit_id: Optional[str],
        finding: Dict[str, Any],
        code_context: str,
        conversation: list[dict[str, Any]],
    ) -> str:
        return dedent(
            f"""
            你是代码扫描结果复核助手。你要根据用户对某个 finding 的反馈，判断该 finding 是否需要维持、降级或判定为模型误判。

            commit: {commit_id or "N/A"}
            原始 finding:
            {json.dumps(finding, ensure_ascii=False, indent=2)}

            代码上下文:
            {code_context}

            沟通记录:
            {json.dumps(conversation, ensure_ascii=False, indent=2)}

            要求:
            1. 认真判断用户观点是否成立。
            2. 若用户有道理但问题仍存在，可选择 downgrade，并给出新的 severity。
            3. 若确认这是模型误判或不应作为当前增量问题，选择 resolve_false_positive。
            4. 若维持原判断，选择 keep。
            5. 只输出一个 JSON 对象，不要输出 Markdown。

            JSON schema:
            {{
              "action": "keep|downgrade|resolve_false_positive",
              "severity": "fatal|high|medium|low|info|null",
              "reply": "string",
              "pattern_summary": "string"
            }}
            """
        ).strip()

    @staticmethod
    def _build_missed_issue_feedback_prompt(
        *,
        branch: str,
        commit_id: Optional[str],
        existing_summary: str,
        missed_issue: str,
    ) -> str:
        return dedent(
            f"""
            你是代码扫描结果复核助手。用户认为当前报告漏掉了一个问题，你要判断这个说法是否成立。

            分支: {branch}
            commit: {commit_id or "N/A"}
            当前报告摘要:
            {existing_summary}

            用户提交的漏判描述:
            {missed_issue}

            要求:
            1. 判断这是否属于本次变更中确实遗漏的重要问题。
            2. 若成立，confirmed=true，并简洁说明理由。
            3. 若不成立，confirmed=false，并简洁说明理由。
            4. 如成立，可给出 pattern_summary 作为后续沉淀的范式摘要。
            5. 只输出 JSON，不要输出 Markdown。

            JSON schema:
            {{
              "confirmed": true,
              "reply": "string",
              "pattern_summary": "string"
            }}
            """
        ).strip()

    @staticmethod
    def _build_fix_conversation_prompt(
        *,
        repo_url: str,
        app_name: str,
        branch: str,
        commit_id: Optional[str],
        stage: str,
        selected_findings: list[dict[str, Any]],
        scope_summary: Optional[str],
        plan_summary: Optional[str],
        conversation: list[dict[str, Any]],
    ) -> str:
        return dedent(
            f"""
            你在协助用户修复代码问题。当前阶段是：{stage}

            应用: {app_name}
            仓库: {repo_url}
            分支: {branch}
            commit: {commit_id or "N/A"}
            选中问题:
            {json.dumps(selected_findings, ensure_ascii=False, indent=2)}

            已确认的问题范围摘要: {scope_summary or "暂无"}
            已确认的修复方案摘要: {plan_summary or "暂无"}
            对话历史:
            {json.dumps(conversation, ensure_ascii=False, indent=2)}

            目标：
            1. 范围和边界没确认清楚时，继续澄清并让用户确认，next_stage=scope_confirmation。
            2. 范围清楚后，逐项说明修复方案并让用户确认，next_stage=plan_confirmation。
            3. 只有用户明确确认可以开始修复时，next_stage=fixing。
            4. 如果根据选中问题和对话内容，修复目标应该落在其他关联仓库，你可以直接给出 target_repo_url 和 target_branch，不要再向用户询问是否切换仓库。
            5. 只有在你无法判断具体目标仓库时，才继续追问缺失信息。

            只输出 JSON：
            {{
              "reply": "中文回复",
              "next_stage": "scope_confirmation | plan_confirmation | fixing",
              "target_repo_url": "需要修复的仓库 URL；若沿用当前仓库则填当前 repo_url",
              "target_branch": "需要修复的目标分支；若沿用当前分支则填当前 branch",
              "scope_summary": "中文摘要",
              "plan_summary": "中文摘要"
            }}
            """
        ).strip()

    @staticmethod
    def _build_fix_apply_prompt(
        *,
        repo_path: Path,
        repo_url: str,
        branch: str,
        commit_id: Optional[str],
        selected_findings: list[dict[str, Any]],
        scope_summary: Optional[str],
        plan_summary: Optional[str],
        conversation: list[dict[str, Any]],
        fix_skill_path: Path,
    ) -> str:
        return dedent(
            f"""
            你要在本地仓库中直接修改代码，并修复指定问题。

            仓库路径: {repo_path}
            仓库: {repo_url}
            目标分支: {branch}
            基线 commit: {commit_id or "N/A"}
            修复 skill: {fix_skill_path}

            只允许处理这些问题：
            {json.dumps(selected_findings, ensure_ascii=False, indent=2)}

            已确认的问题范围:
            {scope_summary or "暂无"}

            已确认的修复方案:
            {plan_summary or "暂无"}

            历史对话:
            {json.dumps(conversation, ensure_ascii=False, indent=2)}

            要求：
            1. 直接修改仓库中的代码，不要输出 patch。
            2. 不要越界修改未确认的功能范围。
            3. 当前 repo_path 就是本轮修复的实际目标仓库工作区，直接在这里修改。
            4. 不要再询问用户是否需要切换到其他仓库。
            5. 修改完成后只输出 JSON。

            JSON:
            {{
              "summary": "中文总结，说明本轮具体修改了什么"
            }}
            """
        ).strip()

    @staticmethod
    def _build_fix_review_prompt(
        *,
        repo_path: Path,
        repo_url: str,
        branch: str,
        commit_id: Optional[str],
        selected_findings: list[dict[str, Any]],
        summary: str,
        scope_summary: Optional[str],
        plan_summary: Optional[str],
    ) -> str:
        return dedent(
            f"""
            你要对刚才的修复做二次复核，判断指定问题是否已经修好。

            仓库路径: {repo_path}
            仓库: {repo_url}
            目标分支: {branch}
            基线 commit: {commit_id or "N/A"}

            目标问题:
            {json.dumps(selected_findings, ensure_ascii=False, indent=2)}

            范围约束:
            {scope_summary or "暂无"}

            修复方案:
            {plan_summary or "暂无"}

            本轮修改摘要:
            {summary}

            约束：
            1. 只评估当前仓库路径中的修改结果。
            2. 当前 repo_path 就是本轮修复的实际目标仓库工作区。

            只输出 JSON：
            {{
              "passed": true,
              "reply": "中文复核结论",
              "remaining_finding_indexes": [0, 1]
            }}
            """
        ).strip()

    @staticmethod
    def _build_repair_prompt(broken_output: str, error_message: str) -> str:
        return dedent(
            f"""
            你上一次输出的结果无法通过 JSON 解析或结构校验，需要你只做格式纠正。

            校验错误:
            {error_message}

            约束:
            1. 只输出一个合法 JSON 对象，不要输出解释、前言、Markdown 或代码块。
            2. 保持原有语义，尽量不要新增问题或改写结论。
            3. `summary`、`title`、`detail`、`suggestion` 必须为中文。
            4. `line` 必须是整数；如果原值是范围如 `74-75`，请取首行 `74`；如果无法确定则省略。
            5. `severity` 只能是 `fatal|high|medium|low|info`。

            目标 JSON schema:
            {{
              "summary": "string",
              "pass_check": true,
              "score": 0,
              "findings": [
                {{
                  "file": "relative/path",
                  "line": 1,
                  "severity": "fatal|high|medium|low|info",
                  "title": "string",
                  "detail": "string",
                  "suggestion": "string"
                }}
              ]
            }}

            待纠正原文:
            {broken_output}
            """
        ).strip()

    @staticmethod
    def _parse_output(stdout: str) -> AnalysisResult:
        text = stdout.strip()
        if not text:
            raise RuntimeError("empty opencode output")

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = OpencodeRunner._parse_event_stream(text)
            if data is None:
                data = OpencodeRunner._extract_json_object(text)
        else:
            OpencodeRunner._raise_for_opencode_error_event(data)

        result = AnalysisResult.model_validate(data)
        result.raw_output = data
        return result

    @staticmethod
    def _parse_event_stream(text: str) -> Optional[Dict[str, Any]]:
        events = []
        text_parts = []
        session_ids = []
        has_invalid_event_line = False
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                has_invalid_event_line = True
                continue
            if not isinstance(event, dict):
                continue
            events.append(event)
            OpencodeRunner._raise_for_opencode_error_event(event)
            OpencodeRunner._append_unique_session_id(session_ids, event.get("sessionID"))
            part = event.get("part")
            if isinstance(part, dict):
                OpencodeRunner._append_unique_session_id(session_ids, part.get("sessionID"))
            if (
                event.get("type") == "text"
                and isinstance(part, dict)
                and part.get("type") == "text"
            ):
                part_text = part.get("text")
                if isinstance(part_text, str) and part_text.strip():
                    text_parts.append(part_text.strip())

        if not events:
            return None

        if not text_parts and session_ids:
            text_parts.extend(OpencodeRunner._load_text_parts_from_opencode_db(session_ids))

        joined_text = "\n".join(text_parts).strip()
        if not joined_text:
            if has_invalid_event_line:
                raise UnrepairableOpencodeOutputError(
                    "incomplete opencode event stream: final text part not found"
                )
            raise UnrepairableOpencodeOutputError("opencode event stream has no final text output")
        try:
            return OpencodeRunner._extract_json_object(joined_text)
        except RuntimeError:
            db_text_parts = OpencodeRunner._load_text_parts_from_opencode_db(session_ids)
            if db_text_parts:
                return OpencodeRunner._extract_json_object("\n".join(db_text_parts))
            raise

    @staticmethod
    def _append_unique_session_id(session_ids: list[str], value: Any) -> None:
        if isinstance(value, str) and value and value not in session_ids:
            session_ids.append(value)

    @staticmethod
    def _extract_session_ids(text: str) -> list[str]:
        session_ids: list[str] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            OpencodeRunner._append_unique_session_id(session_ids, event.get("sessionID"))
            part = event.get("part")
            if isinstance(part, dict):
                OpencodeRunner._append_unique_session_id(session_ids, part.get("sessionID"))
        return session_ids

    @staticmethod
    def _raise_for_opencode_error_event(data: Any) -> None:
        if not isinstance(data, dict) or data.get("type") != "error":
            return
        error = data.get("error")
        message = None
        if isinstance(error, dict):
            error_data = error.get("data")
            if isinstance(error_data, dict):
                message = error_data.get("message")
            message = message or error.get("message") or error.get("name")
        raise UnrepairableOpencodeOutputError(f"opencode error event: {message or data}")

    @staticmethod
    def _load_text_parts_from_opencode_db(session_ids: list[str]) -> list[str]:
        db_path = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
        if not session_ids or not db_path.exists():
            return []

        placeholders = ",".join("?" for _ in session_ids)
        query = (
            "select p.data, m.data as message_data "
            "from part p join message m on p.message_id = m.id "
            f"where p.session_id in ({placeholders}) "
            "order by p.time_created, p.id"
        )
        text_parts: list[str] = []
        try:
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.0) as connection:
                for part_data, message_data in connection.execute(query, session_ids):
                    try:
                        message = json.loads(message_data)
                        part = json.loads(part_data)
                    except (TypeError, json.JSONDecodeError):
                        continue
                    if message.get("role") != "assistant":
                        continue
                    if part.get("type") != "text":
                        continue
                    part_text = part.get("text")
                    if isinstance(part_text, str) and part_text.strip():
                        text_parts.append(part_text.strip())
        except sqlite3.Error:
            logger.exception("failed to recover opencode text parts from db session_ids=%s", session_ids)
        return text_parts

    @staticmethod
    def _load_usage_metrics_from_opencode_db(session_ids: list[str]) -> Dict[str, int]:
        db_path = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
        if not session_ids or not db_path.exists():
            return {
                "calls": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "thinking_tokens": 0,
                "cache_read_tokens": 0,
                "total_tokens": 0,
            }

        totals = {
            "calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "thinking_tokens": 0,
            "cache_read_tokens": 0,
            "total_tokens": 0,
        }
        query = "select data from part where session_id = ? order by time_created, id"
        try:
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.0) as connection:
                for session_id in OpencodeRunner._expand_opencode_session_ids(connection, session_ids):
                    lines = [row[0] for row in connection.execute(query, (session_id,))]
                    usage = OpencodeRunner._extract_usage_metrics("\n".join(lines))
                    if usage["total_tokens"] <= 0:
                        continue
                    totals["calls"] += 1
                    totals["prompt_tokens"] += usage["prompt_tokens"]
                    totals["completion_tokens"] += usage["completion_tokens"]
                    totals["thinking_tokens"] += usage["thinking_tokens"]
                    totals["cache_read_tokens"] += usage["cache_read_tokens"]
                    totals["total_tokens"] += usage["total_tokens"]
        except sqlite3.Error:
            logger.exception("failed to recover opencode usage from db session_ids=%s", session_ids)
        return totals

    @staticmethod
    def _expand_opencode_session_ids(connection: sqlite3.Connection, session_ids: list[str]) -> list[str]:
        expanded: list[str] = []
        seen: set[str] = set()
        pending = [session_id for session_id in session_ids if session_id]

        try:
            has_session_table = connection.execute(
                "select 1 from sqlite_master where type = 'table' and name = 'session'"
            ).fetchone()
        except sqlite3.Error:
            has_session_table = None

        while pending:
            session_id = pending.pop(0)
            if session_id in seen:
                continue
            seen.add(session_id)
            expanded.append(session_id)
            if not has_session_table:
                continue
            try:
                children = [
                    row[0]
                    for row in connection.execute(
                        "select id from session where parent_id = ? order by id",
                        (session_id,),
                    )
                    if row[0]
                ]
            except sqlite3.Error:
                continue
            pending.extend(child for child in children if child not in seen)

        return expanded

    @staticmethod
    def _extract_json_object(text: str) -> Dict[str, Any]:
        text = text.strip()
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

        for start in range(len(text)):
            if text[start] != "{":
                continue
            candidate = OpencodeRunner._find_balanced_json_object(text, start)
            if candidate is None:
                continue
            try:
                data = json.loads(candidate)
            except json.JSONDecodeError:
                repaired = OpencodeRunner._repair_json_candidate(candidate)
                if repaired == candidate:
                    continue
                try:
                    data = json.loads(repaired)
                except json.JSONDecodeError:
                    continue
            if isinstance(data, dict):
                return data

        raise RuntimeError(f"opencode output is not valid json: {text}")

    @staticmethod
    def _extract_usage_metrics(text: str) -> Dict[str, int]:
        candidates: list[Dict[str, int]] = []

        def normalize_int(value: Any) -> Optional[int]:
            if isinstance(value, bool) or value is None:
                return None
            if isinstance(value, int):
                return value
            if isinstance(value, float):
                return int(value)
            if isinstance(value, str) and value.strip().isdigit():
                return int(value.strip())
            return None

        def candidate_from_dict(obj: Dict[str, Any]) -> Optional[Dict[str, int]]:
            lower = {str(key).lower(): value for key, value in obj.items()}
            input_value = normalize_int(
                lower.get("inputtokens")
                or lower.get("prompttokens")
                or lower.get("input_tokens")
                or lower.get("prompt_tokens")
            )
            output_value = normalize_int(
                lower.get("outputtokens")
                or lower.get("completiontokens")
                or lower.get("output_tokens")
                or lower.get("completion_tokens")
            )
            thinking_value = normalize_int(
                lower.get("thinkingtokens")
                or lower.get("reasoningtokens")
                or lower.get("thinking_tokens")
                or lower.get("reasoning_tokens")
            )
            cache_read_value = normalize_int(
                lower.get("cachereadtokens")
                or lower.get("cache_read_tokens")
                or lower.get("cache_read")
                or lower.get("cacheread")
            )
            total_value = normalize_int(lower.get("totaltokens") or lower.get("total_tokens"))
            nested_tokens = lower.get("tokens")
            if isinstance(nested_tokens, dict):
                nested_lower = {str(key).lower(): value for key, value in nested_tokens.items()}
                input_value = input_value if input_value is not None else normalize_int(
                    nested_lower.get("input")
                    or nested_lower.get("prompt")
                    or nested_lower.get("inputtokens")
                    or nested_lower.get("prompttokens")
                )
                output_value = output_value if output_value is not None else normalize_int(
                    nested_lower.get("output")
                    or nested_lower.get("completion")
                    or nested_lower.get("outputtokens")
                    or nested_lower.get("completiontokens")
                )
                thinking_value = thinking_value if thinking_value is not None else normalize_int(
                    nested_lower.get("thinking")
                    or nested_lower.get("reasoning")
                    or nested_lower.get("thinkingtokens")
                    or nested_lower.get("reasoningtokens")
                )
                total_value = total_value if total_value is not None else normalize_int(
                    nested_lower.get("total") or nested_lower.get("totaltokens")
                )
                nested_cache = nested_lower.get("cache")
                if isinstance(nested_cache, dict):
                    cache_lower = {str(key).lower(): value for key, value in nested_cache.items()}
                    cache_read_value = cache_read_value if cache_read_value is not None else normalize_int(
                        cache_lower.get("read")
                        or cache_lower.get("cache_read")
                        or cache_lower.get("cacheread")
                        or cache_lower.get("readtokens")
                    )

            if (
                input_value is None
                and output_value is None
                and thinking_value is None
                and cache_read_value is None
                and total_value is None
            ):
                return None

            prompt_tokens = input_value or 0
            completion_tokens = output_value or 0
            thinking_tokens = thinking_value or 0
            cache_read_tokens = cache_read_value or 0
            total_tokens = total_value or (
                prompt_tokens + completion_tokens + thinking_tokens + cache_read_tokens
            )
            return {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "thinking_tokens": thinking_tokens,
                "cache_read_tokens": cache_read_tokens,
                "total_tokens": total_tokens,
            }

        def scan(obj: Any) -> None:
            if isinstance(obj, dict):
                candidate = candidate_from_dict(obj)
                if candidate is not None:
                    candidates.append(candidate)
                for value in obj.values():
                    scan(value)
            elif isinstance(obj, list):
                for item in obj:
                    scan(item)

        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                scan(json.loads(line))
            except json.JSONDecodeError:
                continue

        if not candidates:
            return {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "thinking_tokens": 0,
                "cache_read_tokens": 0,
                "total_tokens": 0,
            }
        return max(candidates, key=lambda item: item["total_tokens"])

    @staticmethod
    def _find_balanced_json_object(text: str, start: int) -> Optional[str]:
        depth = 0
        in_string = False
        escape = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start : index + 1]
        return None

    @staticmethod
    def _repair_json_candidate(text: str) -> str:
        # Repair common model formatting error: line ranges emitted as 74-75 instead of a JSON string.
        return re.sub(r'("line"\s*:\s*)(\d+)\s*-\s*(\d+)', r'\1"\2-\3"', text)

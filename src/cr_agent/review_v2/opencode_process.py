"""Per-turn OpenCode process runner adapted from comain/unit-test-agent `reference/opencode/process.py`."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from cr_agent.config import Settings
from cr_agent.review_v2.opencode_config import candidate_opencode_models, per_turn_opencode_config_dir, selected_opencode_model
from cr_agent.review_v2.opencode_routing import mark_model_unhealthy
from cr_agent.review_v2.opencode_stream import OpenCodeStreamParser


_PROVIDER_TRANSPORT_FAILURES = (
    "ConnectionRefused",
    "ECONNREFUSED",
    "ENOTFOUND",
    "EAI_AGAIN",
    "ETIMEDOUT",
    "ECONNRESET",
    "fetch failed",
    "socket hang up",
)

_CONNECTION_REFUSED_FAILURES = ("ConnectionRefused", "ECONNREFUSED")


def _model_provider(model_id: Optional[str]) -> str:
    if not model_id or "/" not in model_id:
        return ""
    return model_id.split("/", 1)[0]


def classify_provider_model_error(error_obj: Optional[Dict[str, Any]]) -> Optional[str]:
    """Return a narrow fallback reason for provider/model availability errors."""
    if not error_obj:
        return None
    data = error_obj.get("data") if isinstance(error_obj.get("data"), dict) else {}
    status_code = data.get("statusCode") or data.get("status_code") or error_obj.get("statusCode")
    message_parts = [
        error_obj.get("message"),
        error_obj.get("name"),
        data.get("message"),
        data.get("error"),
        data.get("type"),
    ]
    text = " ".join(str(part) for part in message_parts if part).lower()
    if status_code in {401, 403} or "authentication" in text or "invalid api key" in text:
        return "provider_auth_failed"
    if status_code == 404:
        return "model_not_found"
    if "rate limit" in text or "too many request" in text or "quota exceeded" in text:
        return "rate_limit"
    if "disabled" in text:
        return "model_disabled"
    if "not found" in text or "does not exist" in text or "unknown model" in text:
        return "model_not_found"
    if "unavailable" in text or "not available" in text:
        return "model_unavailable"
    if _contains_provider_transport_failure(error_obj):
        return "provider_error"
    return None


def _contains_provider_transport_failure(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_contains_provider_transport_failure(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_provider_transport_failure(item) for item in value)
    if isinstance(value, str):
        return any(marker in value for marker in _PROVIDER_TRANSPORT_FAILURES)
    return False


def _contains_connection_refused(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_contains_connection_refused(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_connection_refused(item) for item in value)
    if isinstance(value, str):
        return any(marker in value for marker in _CONNECTION_REFUSED_FAILURES)
    return False


def _should_skip_provider_after_failure(provider: str, result: "TurnResult") -> bool:
    return (
        provider == "llm-proxy"
        and result.fallback_reason == "provider_error"
        and _contains_connection_refused(result.error)
    )


@dataclass
class TurnResult:
    type: str
    result: str = ""
    session_id: Optional[str] = None
    model_id: Optional[str] = None
    tokens: Dict[str, Any] = field(default_factory=dict)
    cost_usd: Optional[float] = None
    error: Optional[Dict[str, Any]] = None
    patch_count: int = 0
    fallback_eligible: bool = False
    fallback_reason: Optional[str] = None
    raw_log_path: Optional[str] = None


class OpenCodeProcessRunner:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.parser = OpenCodeStreamParser()

    def run_turn(
        self,
        *,
        prompt_file: Path,
        repo_path: Path,
        model_id: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
        is_cancelled: Optional[Callable[[], bool]] = None,
    ) -> TurnResult:
        prompt = prompt_file.read_text(encoding="utf-8")
        repo_path = repo_path.resolve()
        timeout = timeout_seconds or self.settings.task_timeout_seconds
        label = prompt_file.parent.name if prompt_file.parent.name else "turn"
        models = candidate_opencode_models(self.settings, preferred_model=model_id or selected_opencode_model(self.settings))
        if not models:
            models = [model_id or selected_opencode_model(self.settings)]
        last_result: Optional[TurnResult] = None
        failed_providers: set[str] = set()
        for index, model in enumerate(models):
            provider = _model_provider(model)
            if provider and provider in failed_providers:
                mark_model_unhealthy(model, reason="provider_error")
                continue
            result = self._run_single_turn(
                prompt=prompt,
                repo_path=repo_path,
                model=model,
                timeout=timeout,
                label=label,
                is_cancelled=is_cancelled,
            )
            last_result = result
            has_next = index + 1 < len(models)
            if result.type == "cancelled" or not result.fallback_eligible or not has_next:
                return result
            mark_model_unhealthy(model, reason=result.fallback_reason or "provider_error")
            if provider and _should_skip_provider_after_failure(provider, result):
                failed_providers.add(provider)
        return last_result or TurnResult(type="error", model_id=model_id, error={"message": "no opencode model candidate"})

    def _run_single_turn(
        self,
        *,
        prompt: str,
        repo_path: Path,
        model: str,
        timeout: int,
        label: str,
        is_cancelled: Optional[Callable[[], bool]] = None,
    ) -> TurnResult:
        command = self._build_command(prompt, repo_path=repo_path)
        with per_turn_opencode_config_dir(repo_path, self.settings, label=label, model_id=model) as config_dir:
            process = subprocess.Popen(
                command,
                cwd=str(config_dir),
                env=self._build_env(repo_path=repo_path, model=model),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                **self._popen_kwargs(),
            )
            return self._read_stream(process, repo_path=repo_path, model=model, timeout=timeout, is_cancelled=is_cancelled)

    def _build_command(self, prompt: str, *, repo_path: Path) -> List[str]:
        command = [
            self.settings.opencode_bin,
            "run",
            "--print-logs",
            "--format",
            "json",
        ]
        if self.settings.review_v2_opencode_pure:
            command.append("--pure")
        command.extend(self._message_args(prompt, repo_path=repo_path))
        return command

    def _message_args(self, prompt: str, *, repo_path: Path) -> List[str]:
        threshold = int(self.settings.review_v2_opencode_prompt_file_threshold_chars or 0)
        if len(prompt or "") <= threshold:
            return [prompt]
        prompt_path = self._write_prompt_file(repo_path, prompt)
        return [
            f"Read and follow the attached prompt file exactly: {prompt_path.name}",
            "--file",
            str(prompt_path),
        ]

    @staticmethod
    def _write_prompt_file(repo_path: Path, prompt: str) -> Path:
        root = repo_path / ".cr_agent" / "opencode" / "prompts"
        root.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
        path = root / f"prompt-{digest}.md"
        path.write_text(prompt, encoding="utf-8")
        return path

    def _build_env(self, *, repo_path: Path, model: str) -> Dict[str, str]:
        env = os.environ.copy()
        env["PWD"] = str(repo_path)
        service_bin = str(Path(sys.executable).resolve().parent)
        path_parts = [part for part in (env.get("PATH") or "").split(os.pathsep) if part]
        if service_bin not in path_parts:
            env["PATH"] = os.pathsep.join([service_bin, *path_parts])
        provider = model.split("/", 1)[0] if "/" in model else ""
        from cr_agent.review_v2.opencode_routing import parse_provider_base_urls, parse_provider_tokens

        token = parse_provider_tokens(self.settings.review_v2_opencode_provider_tokens).get(provider, "")
        base_url = parse_provider_base_urls(self.settings.review_v2_opencode_provider_base_urls).get(provider, "")
        if provider == "llm-proxy" and not base_url:
            base_url = self.settings.review_v2_llm_proxy_base_url.rstrip("/")
        if provider == "openai":
            env.pop("OPENAI_API_KEY", None)
            env.pop("OPENAI_BASE_URL", None)
        elif token:
            env["OPENAI_API_KEY"] = token
            if base_url:
                env["OPENAI_BASE_URL"] = base_url
        elif base_url:
            env["OPENAI_BASE_URL"] = base_url
        return env

    def _read_stream(
        self,
        process: subprocess.Popen,
        *,
        repo_path: Path,
        model: str,
        timeout: int,
        is_cancelled: Optional[Callable[[], bool]] = None,
    ) -> TurnResult:
        raw_log_path = self._new_raw_log_path(repo_path)
        raw_log_path.parent.mkdir(parents=True, exist_ok=True)
        events: List[Dict[str, Any]] = []
        stdout_lines: List[str] = []
        stderr_lines: List[str] = []
        lock = threading.Lock()
        progress_lock = threading.Lock()
        stream_started_at = time.time()
        last_event_at = stream_started_at
        saw_event = False
        session_id: Optional[str] = None
        provider_failure: Optional[Dict[str, Any]] = None

        raw_log = raw_log_path.open("a", encoding="utf-8")
        raw_log.write(
            json.dumps(
                {"kind": "turn_start", "timestamp": time.time(), "model_id": model, "timeout": timeout, "pid": process.pid},
                ensure_ascii=False,
            )
            + "\n"
        )
        raw_log.flush()

        def record(stream: str, line: str) -> None:
            event = self.parser.parse_line(line)
            payload = {
                "kind": "stream_line",
                "timestamp": time.time(),
                "stream": stream,
                "raw": line,
                "event_type": event.get("type") if isinstance(event, dict) else None,
                "session_id": event.get("sessionID") if isinstance(event, dict) else None,
            }
            with lock:
                raw_log.write(json.dumps(payload, ensure_ascii=False) + "\n")
                raw_log.flush()

        def mark_progress() -> None:
            nonlocal last_event_at, saw_event
            with progress_lock:
                last_event_at = time.time()
                saw_event = True

        def read_lines(stream_name: str, stream: Any, target: List[str]) -> None:
            nonlocal session_id, provider_failure
            if stream is None:
                return
            for raw in stream:
                line = raw.rstrip()
                target.append(line)
                record(stream_name, line)
                event = self.parser.parse_line(line)
                if event is None:
                    if stream_name == "stderr":
                        failure = self._provider_failure_from_stderr(line)
                        if failure is not None:
                            provider_failure = failure
                            mark_progress()
                    continue
                events.append(event)
                mark_progress()
                if session_id is None:
                    session_id = self.parser.extract_session_id([event])

        stdout_thread = threading.Thread(target=read_lines, args=("stdout", process.stdout, stdout_lines), daemon=True)
        stderr_thread = threading.Thread(target=read_lines, args=("stderr", process.stderr, stderr_lines), daemon=True)
        stdout_thread.start()
        stderr_thread.start()

        base_timeout = max(0.001, float(timeout or 0))
        active_timeout = max(base_timeout, base_timeout * float(self.settings.review_v2_opencode_active_timeout_multiplier or 1.0))
        idle_timeout = max(1.0, float(self.settings.review_v2_opencode_stream_idle_timeout_seconds or base_timeout))
        timed_out = False
        cancelled = False

        while stdout_thread.is_alive() or stderr_thread.is_alive():
            if is_cancelled is not None and is_cancelled():
                cancelled = True
                self._terminate_process(process)
                break
            now = time.time()
            with progress_lock:
                current_saw_event = saw_event
                current_last_event_at = last_event_at
            elapsed = now - stream_started_at
            if provider_failure is not None:
                self._terminate_process(process)
                break
            if (not current_saw_event and elapsed >= base_timeout) or (
                current_saw_event and (elapsed >= active_timeout or now - current_last_event_at >= idle_timeout)
            ):
                timed_out = True
                self._terminate_process(process)
                break
            time.sleep(0.1)

        stdout_thread.join(timeout=0.5)
        stderr_thread.join(timeout=0.5)
        try:
            returncode = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._terminate_process(process)
            returncode = process.poll()

        result = self._build_result(
            events,
            session_id=session_id,
            returncode=returncode,
            stdout="\n".join(stdout_lines),
            stderr="\n".join(stderr_lines),
            timed_out=timed_out,
            cancelled=cancelled,
            provider_failure=provider_failure,
            raw_log_path=raw_log_path,
            model=model,
        )
        raw_log.write(
            json.dumps(
                {
                    "kind": "turn_finish",
                    "timestamp": time.time(),
                    "result_type": result.type,
                    "session_id": result.session_id,
                    "model_id": result.model_id,
                    "tokens": result.tokens,
                    "cost_usd": result.cost_usd,
                    "patch_count": result.patch_count,
                    "fallback_eligible": result.fallback_eligible,
                    "fallback_reason": result.fallback_reason,
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        raw_log.close()
        return result

    def _build_result(
        self,
        events: List[Dict[str, Any]],
        *,
        session_id: Optional[str],
        returncode: Optional[int],
        stdout: str,
        stderr: str,
        timed_out: bool,
        cancelled: bool,
        provider_failure: Optional[Dict[str, Any]],
        raw_log_path: Path,
        model: Optional[str] = None,
    ) -> TurnResult:
        if cancelled:
            return TurnResult(
                type="cancelled",
                result=stdout,
                session_id=session_id or self.parser.extract_session_id(events),
                model_id=model,
                tokens=self.parser.extract_tokens(events),
                cost_usd=self.parser.extract_cost(events),
                error={"message": "task cancelled by operator"},
                patch_count=self.parser.count_patches(events),
                raw_log_path=str(raw_log_path),
            )
        if provider_failure is not None:
            return TurnResult(
                type="error",
                result=stdout,
                session_id=session_id or self.parser.extract_session_id(events),
                model_id=model,
                tokens=self.parser.extract_tokens(events),
                cost_usd=self.parser.extract_cost(events),
                error=provider_failure,
                patch_count=self.parser.count_patches(events),
                fallback_eligible=True,
                fallback_reason="provider_error",
                raw_log_path=str(raw_log_path),
            )
        error_event = next((event for event in events if event.get("type") == "error"), None)
        if error_event is not None:
            error_obj = error_event.get("error") or error_event
            is_rate_limited = self.parser.detect_rate_limit(error_event)
            fallback_reason = "rate_limit" if is_rate_limited else classify_provider_model_error(error_obj)
            return TurnResult(
                type="rate_limited" if is_rate_limited or fallback_reason == "rate_limit" else "error",
                session_id=session_id or self.parser.extract_session_id(events),
                model_id=model,
                tokens=self.parser.extract_tokens(events),
                cost_usd=self.parser.extract_cost(events),
                error=error_obj,
                patch_count=self.parser.count_patches(events),
                fallback_eligible=fallback_reason is not None,
                fallback_reason=fallback_reason,
                raw_log_path=str(raw_log_path),
            )
        if timed_out:
            no_output = not self.parser.extract_text(events) and not self.parser.extract_tokens(events)
            return TurnResult(
                type="timeout",
                session_id=session_id,
                model_id=model,
                tokens=self.parser.extract_tokens(events),
                cost_usd=self.parser.extract_cost(events),
                patch_count=self.parser.count_patches(events),
                fallback_eligible=no_output,
                fallback_reason="no_output" if no_output else None,
                error={"message": "opencode prompt timeout"},
                raw_log_path=str(raw_log_path),
            )
        if returncode != 0:
            return TurnResult(
                type="error",
                result=stdout,
                session_id=session_id or self.parser.extract_session_id(events),
                model_id=model,
                tokens=self.parser.extract_tokens(events),
                cost_usd=self.parser.extract_cost(events),
                error={"message": stderr or stdout, "returncode": returncode},
                patch_count=self.parser.count_patches(events),
                raw_log_path=str(raw_log_path),
            )
        return TurnResult(
            type="completed",
            result=self.parser.extract_text(events),
            session_id=session_id or self.parser.extract_session_id(events),
            model_id=model,
            tokens=self.parser.extract_tokens(events),
            cost_usd=self.parser.extract_cost(events),
            patch_count=self.parser.count_patches(events),
            raw_log_path=str(raw_log_path),
        )

    @staticmethod
    def _provider_failure_from_stderr(line: str) -> Optional[Dict[str, Any]]:
        if "AI_APICallError" not in line and "AI_RetryError" not in line:
            return None
        if not any(marker in line for marker in _PROVIDER_TRANSPORT_FAILURES):
            return None
        return {
            "message": "opencode provider transport failure",
            "stderr": line[:4000],
        }

    def _new_raw_log_path(self, repo_path: Path) -> Path:
        log_root = self.settings.review_v2_audit_dir / "opencode_turns"
        repo_name = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in repo_path.name) or "repo"
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        return log_root / f"{timestamp}_{repo_name}.jsonl"

    @staticmethod
    def _popen_kwargs() -> Dict[str, Any]:
        if os.name == "nt":
            return {}

        def preexec() -> None:
            os.setsid()
            if not sys.platform.startswith("linux"):
                return
            try:
                import ctypes

                libc = ctypes.CDLL(None)
                libc.prctl(1, signal.SIGTERM)
                if os.getppid() == 1:
                    os.kill(os.getpid(), signal.SIGTERM)
            except Exception:
                return

        return {"preexec_fn": preexec}

    @staticmethod
    def _terminate_process(process: subprocess.Popen, *, grace_seconds: float = 2.0) -> None:
        if os.name != "nt":
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                process.wait(timeout=grace_seconds)
                return
            except ProcessLookupError:
                return
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                return
            except Exception:
                pass
        try:
            process.terminate()
            process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

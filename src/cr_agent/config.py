from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CR_AGENT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = "0.0.0.0"
    port: int = 8000
    worker_threads: int = 4
    queue_size: int = 128
    task_timeout_seconds: int = 1200
    callback_timeout_seconds: int = 10
    callback_retry_times: int = 3
    trigger_token: str = ""
    llm_retry_times: int = 2
    llm_format_retry_times: int = 2
    max_task_attempts: int = 3
    fix_worker_threads: int = 2
    orphan_task_timeout_seconds: int = 1800
    recovery_scan_interval_seconds: int = 30
    git_clone_depth: int = 50
    git_ssh_key_path: str = ""
    git_access_token: str = Field(
        default="",
        validation_alias=AliasChoices("GIT_AC", "CR_AGENT_GIT_ACCESS_TOKEN"),
    )
    ci_ack_url: str = "http://127.0.0.1/ci/plugin/ack"
    ci_notice_url: str = ""
    review_base_refs: str = "origin/master"

    base_dir: Path = Field(default=Path("runtime"))
    repo_cache_dir: Path = Field(default=Path("runtime/repos"))
    task_dir: Path = Field(default=Path("runtime/tasks"))
    report_dir: Path = Field(default=Path("runtime/reports"))
    usage_dir: Path = Field(default=Path("runtime/usage"))
    log_dir: Path = Field(default=Path("logs"))
    issues_dir: Path = Field(default=Path("/opt/app/issues"))
    fix_space_dir: Path = Field(default=Path("/opt/app/fix_space"))

    report_base_url: str = "http://127.0.0.1/reports"
    report_public_root: str = "/reports"
    gitlab_base_url: str = "https://github.com"
    gitlab_api_token: str = ""
    fix_loop_max_rounds: int = 3

    opencode_bin: str = "opencode"
    opencode_command_template: str = (
        "cat {prompt_file} | {opencode_bin} run "
        "--dir {repo_path} --format json -m llm-proxy/gpt-5.5"
    )

    fix_skill_dir: Path = Field(default=Path("skills/llm_fix"))

    review_v2_db_path: Optional[Path] = None
    review_v2_audit_dir: Optional[Path] = None
    review_v2_daemon_id: str = ""
    review_v2_daemon_poll_interval_seconds: float = 2.0
    review_v2_daemon_claim_limit: int = 1
    review_v2_daemon_lease_seconds: int = 300
    review_v2_daemon_once: bool = False
    review_v2_reviewer_concurrency: int = 3
    review_v2_global_opencode_concurrency: int = 4
    review_v2_context_max_bytes: int = 900_000
    review_v2_prompt_max_bytes: int = 1_200_000
    review_v2_raw_log_max_bytes: int = 20_000_000
    review_v2_callback_max_attempts: int = 5
    review_v2_callback_initial_delay_seconds: int = 30
    review_v2_callback_max_delay_seconds: int = 900
    review_v2_feedback_pattern_sync_enabled: bool = True
    review_v2_feedback_pattern_sync_hour: int = 23
    review_v2_feedback_pattern_sync_minute: int = 38
    review_v2_feedback_pattern_sync_limit: int = 200
    review_v2_feedback_pattern_output_path: Optional[Path] = None
    review_v2_feedback_pattern_sync_target_branch: str = "init"
    review_v2_feedback_pattern_sync_work_root: Optional[Path] = None
    review_v2_llm_proxy_base_url: str = "http://openai-compatible.example.com/v1"
    review_v2_opencode_provider_chain: str = "llm-proxy:gpt-5.5"
    review_v2_opencode_provider_tokens: str = ""
    review_v2_opencode_provider_base_urls: str = "llm-proxy.base_url=http://openai-compatible.example.com/v1"
    review_v2_opencode_model_api_timeout_seconds: float = 5.0
    review_v2_opencode_model_api_cache_seconds: int = 300
    review_v2_opencode_pure: bool = True
    review_v2_opencode_prompt_file_threshold_chars: int = 60_000
    review_v2_opencode_stream_idle_timeout_seconds: float = 180.0
    review_v2_opencode_active_timeout_multiplier: float = 2.0
    review_v2_enabled: bool = False

    def model_post_init(self, __context: Any) -> None:
        if self.review_v2_db_path is None:
            self.review_v2_db_path = self.base_dir / "review_v2.sqlite3"
        if self.review_v2_audit_dir is None:
            self.review_v2_audit_dir = self.base_dir / "review_v2_audit"

    def ensure_dirs(self) -> None:
        for path in (
            self.base_dir,
            self.repo_cache_dir,
            self.task_dir,
            self.report_dir,
            self.usage_dir,
            self.log_dir,
            self.review_v2_audit_dir,
        ):
            if path is None:
                continue
            path.mkdir(parents=True, exist_ok=True)
        if self.review_v2_db_path is not None:
            self.review_v2_db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.fix_space_dir.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            self.fix_space_dir = self.base_dir / "fix_space"
            self.fix_space_dir.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings

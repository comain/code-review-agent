from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from pydantic import AliasChoices, BaseModel, Field, HttpUrl, field_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TaskStatus(str, Enum):
    queued = "queued"
    running = "running"
    success = "success"
    failed = "failed"


class FindingSeverity(str, Enum):
    fatal = "fatal"
    high = "high"
    medium = "medium"
    low = "low"
    info = "info"


class FindingStatus(str, Enum):
    open = "open"
    severity_adjusted = "severity_adjusted"
    resolved_model_false_positive = "resolved_model_false_positive"


class FixSessionStage(str, Enum):
    scope_confirmation = "scope_confirmation"
    plan_confirmation = "plan_confirmation"
    fixing = "fixing"
    awaiting_user_confirmation = "awaiting_user_confirmation"
    completed = "completed"
    failed = "failed"


class Finding(BaseModel):
    file: str = Field(..., description="Relative path in repository")
    line: Optional[int] = None
    severity: FindingSeverity
    title: str
    detail: str
    suggestion: Optional[str] = None

    @field_validator("line", mode="before")
    @classmethod
    def normalize_line(cls, value: Any) -> Any:
        if value is None or value == "":
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            match = re.match(r"\s*(\d+)", value)
            if match:
                return int(match.group(1))
        return value


class AnalysisResult(BaseModel):
    summary: str
    pass_check: bool
    score: int = Field(..., ge=0, le=100)
    findings: List[Finding] = Field(default_factory=list)
    raw_output: Optional[Dict[str, Any]] = None


class FeedbackRole(str, Enum):
    user = "user"
    model = "model"


class FeedbackMessage(BaseModel):
    role: FeedbackRole
    content: str
    created_at: datetime = Field(default_factory=utc_now)


class FindingFeedbackThread(BaseModel):
    finding_index: int
    status: FindingStatus = FindingStatus.open
    current_severity: Optional[FindingSeverity] = None
    pattern_summary: Optional[str] = None
    messages: List[FeedbackMessage] = Field(default_factory=list)
    processing: bool = False
    updated_at: datetime = Field(default_factory=utc_now)


class GeneralFeedbackItem(BaseModel):
    feedback_id: str
    content: str
    processing: bool = False
    confirmed: Optional[bool] = None
    reply: Optional[str] = None
    pattern_summary: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class FixSession(BaseModel):
    session_id: str
    selected_finding_indexes: List[int] = Field(default_factory=list)
    stage: FixSessionStage = FixSessionStage.scope_confirmation
    target_repo_url: Optional[str] = None
    target_branch: Optional[str] = None
    scope_summary: Optional[str] = None
    plan_summary: Optional[str] = None
    messages: List[FeedbackMessage] = Field(default_factory=list)
    processing: bool = False
    workspace_dir: Optional[str] = None
    source_branch: Optional[str] = None
    merge_request_url: Optional[str] = None
    last_result: Optional[str] = None
    execution_round: int = 0
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class FindingView(BaseModel):
    index: int
    file: str
    line: Optional[int] = None
    title: str
    detail: str
    suggestion: Optional[str] = None
    original_severity: FindingSeverity
    effective_severity: Optional[FindingSeverity] = None
    status: FindingStatus = FindingStatus.open
    status_label: str = "待处理"
    processing: bool = False
    gitlab_url: Optional[str] = None
    thread: FindingFeedbackThread = Field(default_factory=lambda: FindingFeedbackThread(finding_index=0))


class FixSessionView(BaseModel):
    session_id: str
    stage: FixSessionStage
    stage_label: str
    selected_finding_indexes: List[int] = Field(default_factory=list)
    selected_finding_titles: List[str] = Field(default_factory=list)
    target_repo_url: Optional[str] = None
    target_branch: Optional[str] = None
    scope_summary: Optional[str] = None
    plan_summary: Optional[str] = None
    processing: bool = False
    workspace_dir: Optional[str] = None
    source_branch: Optional[str] = None
    merge_request_url: Optional[str] = None
    last_result: Optional[str] = None
    execution_round: int = 0
    messages: List[FeedbackMessage] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ReportDetail(BaseModel):
    task_id: str
    app_name: str
    branch: str
    commit_id: Optional[str] = None
    generated_at: Optional[datetime] = None
    summary: str
    score: int
    pass_check: bool
    report_url: Optional[str] = None
    findings: List[FindingView] = Field(default_factory=list)
    general_feedbacks: List[GeneralFeedbackItem] = Field(default_factory=list)
    fix_sessions: List[FixSessionView] = Field(default_factory=list)


class TriggerRequest(BaseModel):
    app_name: str = Field(validation_alias=AliasChoices("app_name", "appName"))
    repo_url: str = Field(validation_alias=AliasChoices("repo_url", "repoUrl", "gitUrl"))
    branch: str = Field(validation_alias=AliasChoices("branch", "gitBranchName"))
    commit_id: Optional[str] = None
    callback_url: Optional[HttpUrl] = None
    callback_token: Optional[str] = None
    pipeline_id: Optional[str] = None
    sprint_id: Optional[str] = None
    operator: Optional[str] = None
    trigger_source: str = "manual"
    metadata: Dict[str, Any] = Field(default_factory=dict)
    ci_task_id: Optional[str] = Field(default=None, validation_alias=AliasChoices("ci_task_id", "taskId"))
    ci_record_id: Optional[str] = Field(default=None, validation_alias=AliasChoices("ci_record_id", "recordId"))
    ci_parent_id: Optional[str] = Field(default=None, validation_alias=AliasChoices("ci_parent_id", "parentId"))
    ci_task_template_id: Optional[str] = Field(default=None, validation_alias=AliasChoices("ci_task_template_id", "taskTemplateId"))

    @field_validator("app_name", "repo_url", "branch")
    @classmethod
    def non_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

    @field_validator("repo_url")
    @classmethod
    def safe_repo_url(cls, value: str) -> str:
        if value.startswith("-"):
            raise ValueError("unsafe git repo_url")
        parsed = urlparse(value)
        if parsed.scheme:
            if parsed.scheme not in {"https", "ssh"}:
                raise ValueError("unsupported git repo_url scheme")
            if not parsed.hostname:
                raise ValueError("git repo_url host is required")
            return value
        if re.match(r"^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[^\\\s]+$", value):
            return value
        raise ValueError("unsupported git repo_url format")

    @field_validator("branch")
    @classmethod
    def safe_branch(cls, value: str) -> str:
        _validate_git_ref(value, "branch")
        branch = value.removeprefix("origin/")
        if branch.startswith("/") or branch.endswith("/") or branch.endswith(".") or branch.endswith(".lock"):
            raise ValueError("unsafe git branch")
        return value

    @field_validator("commit_id")
    @classmethod
    def safe_commit_id(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = value.strip()
        if not value:
            return None
        _validate_git_ref(value, "commit_id")
        return value

    @classmethod
    def from_ci_payload(cls, payload: Dict[str, Any]) -> "TriggerRequest":
        attribute = payload.get("attribute") or {}
        metadata = payload.get("data") or {}

        def pick(*values: Any) -> Any:
            for value in values:
                if value is not None:
                    return value
            return None

        def as_str(value: Any) -> Optional[str]:
            if value is None:
                return None
            return str(value)

        return cls.model_validate(
            {
                "appName": pick(attribute.get("appName"), payload.get("appName")),
                "gitUrl": pick(
                    attribute.get("gitUrl"),
                    attribute.get("gitRepositoryPath"),
                    payload.get("gitUrl"),
                    payload.get("gitRepositoryPath"),
                ),
                "branch": pick(attribute.get("branch"), payload.get("branch")),
                "taskId": as_str(pick(attribute.get("taskId"), payload.get("taskId"))),
                "recordId": as_str(pick(attribute.get("recordId"), payload.get("recordId"))),
                "parentId": as_str(pick(attribute.get("parentId"), payload.get("parentId"))),
                "taskTemplateId": as_str(pick(attribute.get("taskTemplateId"), payload.get("taskTemplateId"))),
                "operator": pick(payload.get("operator"), attribute.get("operator")),
                "trigger_source": pick(payload.get("trigger_source"), attribute.get("trigger_source"), "ci"),
                "metadata": metadata,
            }
        )


def _validate_git_ref(value: str, field_name: str) -> None:
    if not value or value.startswith("-"):
        raise ValueError(f"unsafe git {field_name}")
    if any(ord(ch) < 32 or ch.isspace() for ch in value):
        raise ValueError(f"unsafe git {field_name}")
    forbidden = ("..", "~", "^", ":", "?", "*", "[", "\\", "@{", "//")
    if any(item in value for item in forbidden):
        raise ValueError(f"unsafe git {field_name}")


class TaskRecord(BaseModel):
    task_id: str
    status: TaskStatus
    request: TriggerRequest
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    attempts: int = 0
    error_message: Optional[str] = None
    report_url: Optional[str] = None
    report_file: Optional[str] = None
    result: Optional[AnalysisResult] = None
    opencode_session_ids: List[str] = Field(default_factory=list)
    callback_history: List[Dict[str, Any]] = Field(default_factory=list)
    callback_succeeded: bool = False
    callback_payload_digest: Optional[str] = None

    def touch(self) -> None:
        self.updated_at = utc_now()


class TriggerResponse(BaseModel):
    task_id: str
    status: TaskStatus
    task_url: Optional[str] = None
    report_url: Optional[str] = None


class CallbackPayload(BaseModel):
    task_id: str
    app_name: str
    branch: str
    commit_id: Optional[str] = None
    passed: bool
    score: int
    report_url: str
    status: TaskStatus
    summary: str
    findings_count: int
    generated_at: datetime = Field(default_factory=utc_now)
    metadata: Dict[str, Any] = Field(default_factory=dict)

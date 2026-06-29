from pathlib import Path
from unittest.mock import patch

from cr_agent.core.service import TaskService
from cr_agent.models import AnalysisResult, FeedbackMessage, FeedbackRole, FixSession, FixSessionStage
from cr_agent.models import TaskRecord, TaskStatus, TriggerRequest

from test_service import build_settings


def test_submit_fix_session_message_retries_failed_session(tmp_path: Path) -> None:
    service = TaskService(build_settings(tmp_path))
    request = TriggerRequest.model_validate(
        {
            "app_name": "demo",
            "repo_url": "git@github.com:comain/code-review-agent.git",
            "branch": "feature/test",
        }
    )
    record = TaskRecord(
        task_id="task1",
        status=TaskStatus.failed,
        request=request,
        result=AnalysisResult.model_validate(
            {
                "summary": "摘要",
                "pass_check": False,
                "score": 80,
                "findings": [
                    {
                        "file": "src/main/java/com/foo/Demo.java",
                        "line": 12,
                        "severity": "medium",
                        "title": "issue",
                        "detail": "detail",
                    }
                ],
            }
        ),
    )
    service.store.save(record)
    session = FixSession(
        session_id="s1",
        selected_finding_indexes=[0],
        stage=FixSessionStage.failed,
        processing=False,
        last_result="修复处理失败：boom",
        messages=[FeedbackMessage(role=FeedbackRole.model, content="修复处理失败：boom")],
    )
    service.feedback.save_fix_sessions("task1", [session])

    updated = service.submit_fix_session_message("task1", "s1", "请重试")

    assert updated.stage == FixSessionStage.scope_confirmation
    assert updated.processing is True
    assert updated.messages[-1].role == FeedbackRole.user
    assert updated.messages[-1].content == "请重试"


def test_process_fix_job_persists_target_repo_decision(tmp_path: Path) -> None:
    service = TaskService(build_settings(tmp_path))
    request = TriggerRequest.model_validate(
        {
            "app_name": "demo",
            "repo_url": "git@github.com:comain/code-review-agent.git",
            "branch": "feature/test",
        }
    )
    record = TaskRecord(
        task_id="task2",
        status=TaskStatus.failed,
        request=request,
        result=AnalysisResult.model_validate(
            {
                "summary": "摘要",
                "pass_check": False,
                "score": 80,
                "findings": [
                    {
                        "file": "src/main/java/com/foo/Demo.java",
                        "line": 12,
                        "severity": "medium",
                        "title": "issue",
                        "detail": "detail",
                    }
                ],
            }
        ),
    )
    service.store.save(record)
    session = FixSession(
        session_id="s2",
        selected_finding_indexes=[0],
        stage=FixSessionStage.scope_confirmation,
        processing=True,
        messages=[FeedbackMessage(role=FeedbackRole.user, content="请直接修复")],
    )
    service.feedback.save_fix_sessions("task2", [session])

    with patch.object(
        service.runner,
        "review_fix_conversation",
        return_value={
            "reply": "已确认目标仓库，准备修复。",
            "next_stage": "plan_confirmation",
            "target_repo_url": "git@github.com:comain/code-review-agent.git",
            "target_branch": "DEMO-11317",
            "scope_summary": "只修复 1 个问题",
            "plan_summary": "删除调试日志",
        },
    ):
        service._process_fix_job("task2", "s2")

    saved = service.feedback.load_fix_sessions("task2")[0]
    assert saved.target_repo_url == "git@github.com:comain/code-review-agent.git"
    assert saved.target_branch == "DEMO-11317"
    assert saved.stage == FixSessionStage.plan_confirmation

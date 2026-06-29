from cr_agent.models import AnalysisResult, FindingSeverity
from cr_agent.models import TriggerRequest
import pytest


def test_analysis_result_validation() -> None:
    result = AnalysisResult.model_validate(
        {
            "summary": "ok",
            "pass_check": False,
            "score": 80,
            "findings": [
                {
                    "file": "src/app.py",
                    "line": 12,
                    "severity": "high",
                    "title": "timeout missing",
                    "detail": "callback has no timeout",
                    "suggestion": "add timeout",
                }
            ],
        }
    )

    assert result.score == 80
    assert result.findings[0].severity == FindingSeverity.high


def test_analysis_result_accepts_line_range_string() -> None:
    result = AnalysisResult.model_validate(
        {
            "summary": "ok",
            "pass_check": False,
            "score": 80,
            "findings": [
                {
                    "file": "src/app.py",
                    "line": "74-75",
                    "severity": "high",
                    "title": "timeout missing",
                    "detail": "callback has no timeout",
                    "suggestion": "add timeout",
                }
            ],
        }
    )

    assert result.findings[0].line == 74


def test_trigger_request_supports_ci_payload() -> None:
    req = TriggerRequest.from_ci_payload(
        {
            "operator": "reviewer",
            "attribute": {
                "appName": "w_trade_demo",
                "branch": "feature/test",
                "gitUrl": "git@github.com:comain/code-review-agent.git",
                "taskId": "100",
                "recordId": "200",
                "parentId": "300",
                "taskTemplateId": "T_90_pre_llmScanAppTool",
            },
        }
    )

    assert req.app_name == "w_trade_demo"
    assert req.repo_url.endswith("code-review-agent.git")
    assert req.ci_task_id == "100"


@pytest.mark.parametrize(
    "repo_url",
    [
        "ext::sh -c 'id'",
        "file:///tmp/repo",
        "--upload-pack=/bin/sh",
        "/tmp/repo",
    ],
)
def test_trigger_request_rejects_unsafe_repo_url(repo_url: str) -> None:
    with pytest.raises(ValueError):
        TriggerRequest.model_validate({"app_name": "demo", "repo_url": repo_url, "branch": "feature/test"})


@pytest.mark.parametrize("branch", ["--upload-pack=/bin/sh", "feature test", "feature:ref", "feature..main", "feature@{1}"])
def test_trigger_request_rejects_unsafe_branch(branch: str) -> None:
    with pytest.raises(ValueError):
        TriggerRequest.model_validate(
            {"app_name": "demo", "repo_url": "git@github.com:comain/code-review-agent.git", "branch": branch}
        )

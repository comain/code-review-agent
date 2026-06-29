from cr_agent.review_v2.context import ChangedFileSummary
from cr_agent.review_v2.risk import classify_risk, plan_reviewers


def test_ignored_only_change_is_skipped() -> None:
    risk = classify_risk(
        changed_files=[ChangedFileSummary(path="README.md", changed_lines=2, production=False, ignored=True)],
        diff_bytes=50,
        truncated=False,
    )

    assert risk.tier == "skipped"
    assert plan_reviewers(risk) == []


def test_test_only_change_is_skipped() -> None:
    risk = classify_risk(
        changed_files=[
            ChangedFileSummary(
                path="src/test/java/com/demo/AppTest.java",
                changed_lines=8,
                production=False,
                ignored=False,
                test=True,
                skip_reason="test file",
            )
        ],
        diff_bytes=500,
        truncated=False,
    )

    assert risk.tier == "skipped"
    assert risk.reasons == ["test files only"]
    assert plan_reviewers(risk) == []


def test_bloat_only_change_is_skipped() -> None:
    risk = classify_risk(
        changed_files=[
            ChangedFileSummary(
                path="web/static/app.min.js",
                changed_lines=1,
                production=False,
                ignored=True,
                bloat=True,
                skip_reason="bloat file type",
            )
        ],
        diff_bytes=50_000,
        truncated=False,
    )

    assert risk.tier == "skipped"
    assert risk.reasons == ["bloat files only"]
    assert plan_reviewers(risk) == []


def test_light_production_change_uses_correctness_light() -> None:
    risk = classify_risk(
        changed_files=[ChangedFileSummary(path="src/app.py", changed_lines=3, production=True, ignored=False)],
        diff_bytes=200,
        truncated=False,
    )
    plan = plan_reviewers(risk)

    assert risk.tier == "light"
    assert [(item.reviewer, item.required) for item in plan] == [("correctness_light", True)]


def test_security_trigger_forces_full_specialist_plan() -> None:
    risk = classify_risk(
        changed_files=[ChangedFileSummary(path="src/auth/token_service.py", changed_lines=10, production=True, ignored=False)],
        diff_bytes=500,
        truncated=False,
    )
    plan = plan_reviewers(risk)

    assert risk.tier == "full"
    assert "security" in risk.specialists
    assert [(item.reviewer, item.required) for item in plan] == [
        ("correctness", True),
        ("security", True),
        ("api_contract", True),
        ("config_release", True),
        ("performance", True),
    ]


def test_dubbo_rpc_provider_api_change_triggers_api_contract() -> None:
    risk = classify_risk(
        changed_files=[
            ChangedFileSummary(
                path="provider-api/src/main/java/com/demo/order/remote/OrderRemote.java",
                changed_lines=4,
                production=True,
                ignored=False,
            )
        ],
        diff_bytes=400,
        truncated=False,
    )
    plan = plan_reviewers(risk)

    assert risk.tier == "full"
    assert risk.specialists == ["api_contract"]
    assert "api_contract trigger" in risk.reasons
    assert [(item.reviewer, item.required) for item in plan] == [
        ("correctness", True),
        ("security", True),
        ("api_contract", True),
        ("config_release", True),
        ("performance", True),
    ]


def test_full_review_without_path_specialist_runs_all_ci_axes() -> None:
    risk = classify_risk(
        changed_files=[ChangedFileSummary(path="src/app.py", changed_lines=900, production=True, ignored=False)],
        diff_bytes=1000,
        truncated=False,
    )
    plan = plan_reviewers(risk)

    assert risk.tier == "full"
    assert [(item.reviewer, item.required) for item in plan] == [
        ("correctness", True),
        ("security", True),
        ("api_contract", True),
        ("config_release", True),
        ("performance", True),
    ]

from cr_agent.review_v2.guards import prepare_review_guard
from cr_agent.review_v2.risk import RiskResult


def test_prepare_guard_routes_ignored_change_to_skipped() -> None:
    assert prepare_review_guard(RiskResult(tier="skipped", reasons=["ignored files only"], specialists=[])) == {
        "outcome": "skipped",
        "gate_status": "skipped",
    }


def test_prepare_guard_routes_context_too_large_to_failure() -> None:
    assert prepare_review_guard(
        RiskResult(tier="full", reasons=["context truncated"], specialists=[], context_too_large=True)
    ) == {
        "outcome": "fail",
        "gate_status": "context_too_large",
    }

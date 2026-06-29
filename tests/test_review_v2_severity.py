import pytest

from cr_agent.review_v2.models import ReviewSeverity, normalize_severity


def test_normalize_severity_maps_critical_to_fatal() -> None:
    assert normalize_severity("critical") == ReviewSeverity.fatal
    assert normalize_severity("CRITICAL") == ReviewSeverity.fatal


def test_normalize_severity_rejects_unknown_values() -> None:
    with pytest.raises(ValueError, match="unsupported severity"):
        normalize_severity("blocker")

from cr_agent.review_v2.judge import JudgeNormalizer


def test_judge_normalizes_severity_dedupes_and_rejects_schema_incomplete() -> None:
    result = JudgeNormalizer().normalize(
        reviewer_outputs=[
            {
                "review_run_id": 1,
                "reviewer": "security",
                "findings": [
                    {
                        "file": "src/auth.py",
                        "line": 9,
                        "severity": "critical",
                        "title": "Token leak",
                        "detail": "secret is logged",
                        "confidence": 0.9,
                    },
                    {
                        "file": "src/auth.py",
                        "line": 9,
                        "severity": "high",
                        "title": "Token leak",
                        "detail": "secret is logged again",
                        "confidence": 0.8,
                    },
                    {"file": "src/auth.py", "line": 11, "severity": "low"},
                ],
            }
        ],
        changed_lines={"src/auth.py": [9]},
        risk_tier="full",
    )

    assert len(result.accepted_findings) == 1
    assert result.accepted_findings[0]["severity"] == "fatal"
    assert result.accepted_findings[0]["finding_id"].startswith("fnd_")
    assert result.rejected_candidates[0]["reason"] == "duplicate_merged"
    assert result.rejected_candidates[1]["reason"] == "schema_incomplete"


def test_judge_rejects_low_confidence_candidate() -> None:
    result = JudgeNormalizer().normalize(
        reviewer_outputs=[
            {
                "review_run_id": 1,
                "reviewer": "correctness",
                "findings": [
                    {
                        "file": "src/app.py",
                        "line": 1,
                        "severity": "medium",
                        "title": "Maybe bug",
                        "detail": "speculative",
                        "confidence": 0.2,
                    }
                ],
            }
        ],
        changed_lines={"src/app.py": [1]},
        risk_tier="light",
    )

    assert result.accepted_findings == []
    assert result.rejected_candidates[0]["reason"] == "low_confidence"

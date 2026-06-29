"""Deterministic workflow guards for CR v2."""

from __future__ import annotations

from cr_agent.review_v2.risk import RiskResult


def prepare_review_guard(risk: RiskResult) -> dict:
    if risk.tier == "skipped":
        return {"outcome": "skipped", "gate_status": "skipped"}
    if risk.context_too_large:
        return {"outcome": "fail", "gate_status": "context_too_large"}
    return {"outcome": "review", "gate_status": "review_required"}

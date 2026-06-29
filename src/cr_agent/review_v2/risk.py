"""Deterministic CR v2 risk tiering and reviewer planning."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, List


@dataclass(frozen=True)
class RiskResult:
    tier: str
    reasons: List[str]
    specialists: List[str]
    context_too_large: bool = False


@dataclass(frozen=True)
class ReviewerPlanItem:
    reviewer: str
    required: bool
    reason: str


FULL_CI_REVIEWERS = ["security", "api_contract", "config_release", "performance"]


def classify_risk(*, changed_files: Iterable[Any], diff_bytes: int, truncated: bool) -> RiskResult:
    files = list(changed_files)
    production_files = [item for item in files if item.production]
    changed_line_count = sum(int(item.changed_lines or 0) for item in production_files)
    if not files:
        return RiskResult(tier="skipped", reasons=["no changed files"], specialists=[])
    if not production_files:
        return RiskResult(tier="skipped", reasons=[_non_production_skip_reason(files)], specialists=[])

    specialists = _specialists_for_paths([item.path for item in production_files])
    reasons: List[str] = []
    context_too_large = bool(truncated)
    if truncated:
        reasons.append("context truncated")
    if len(production_files) > 30:
        reasons.append("many production files")
    if changed_line_count > 800:
        reasons.append("large changed-line count")
    if diff_bytes > 500 * 1024:
        reasons.append("large diff")
    if specialists:
        reasons.extend(f"{name} trigger" for name in specialists)
    if context_too_large or reasons:
        return RiskResult(tier="full", reasons=reasons or ["full review trigger"], specialists=specialists, context_too_large=context_too_large)
    if len(production_files) <= 5 and changed_line_count <= 120 and diff_bytes <= 80 * 1024:
        return RiskResult(tier="light", reasons=["small production diff"], specialists=[])
    return RiskResult(tier="standard", reasons=["standard production diff"], specialists=specialists[:2])


def plan_reviewers(risk: RiskResult) -> List[ReviewerPlanItem]:
    if risk.tier == "skipped":
        return []
    if risk.tier == "light":
        return [ReviewerPlanItem("correctness_light", True, "light production diff")]
    required = [ReviewerPlanItem("correctness", True, f"{risk.tier} production diff")]
    if risk.tier == "full":
        required.extend(ReviewerPlanItem(name, True, f"{name} CI axis") for name in FULL_CI_REVIEWERS)
    else:
        required.extend(ReviewerPlanItem(name, False, f"{name} deterministic trigger") for name in risk.specialists[:2])
    return required


def _specialists_for_paths(paths: List[str]) -> List[str]:
    ordered: List[str] = []

    def add(name: str) -> None:
        if name not in ordered:
            ordered.append(name)

    for path in paths:
        lowered = path.lower()
        if any(token in lowered for token in ("token", "secret", "password", "auth", "permission", "api_key")):
            add("security")
        if _is_api_contract_path(lowered):
            add("api_contract")
        if any(token in lowered for token in ("config", "env", "deploy", "supervisor", "callback", "ack")):
            add("config_release")
        if any(token in lowered for token in ("scheduler", "worker", "concurrency", "db", "query", "usage")):
            add("performance")
    priority = ["security", "api_contract", "config_release", "performance"]
    return [name for name in priority if name in ordered]


def _is_api_contract_path(lowered_path: str) -> bool:
    if any(token in lowered_path for token in ("/api/", "routes", "models", "schemas", "templates/report")):
        return True
    parts = [part for part in lowered_path.split("/") if part]
    if any(part in {"remote", "facade", "rpc", "dubbo"} for part in parts):
        return True
    if any(part.endswith(("-api", "_api")) for part in parts):
        return True
    filename = parts[-1] if parts else lowered_path
    return filename.endswith(("remote.java", "facade.java", "rpc.java"))


def _non_production_skip_reason(files: List[Any]) -> str:
    if all(getattr(item, "test", False) for item in files):
        return "test files only"
    if all(getattr(item, "bloat", False) for item in files):
        return "bloat files only"
    if all(getattr(item, "ignored", False) and not getattr(item, "test", False) for item in files):
        return "ignored files only"
    return "non-production files only"

"""Deterministic finding normalization for the initial CR v2 judge path."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Any, Dict, List

from cr_agent.review_v2.models import normalize_severity


_SEVERITY_RANK = {"fatal": 5, "high": 4, "medium": 3, "low": 2, "info": 1}


@dataclass(frozen=True)
class JudgeResult:
    accepted_findings: List[Dict[str, Any]]
    rejected_candidates: List[Dict[str, Any]]
    accepted_empty_rationale: str = ""


class JudgeNormalizer:
    def normalize(
        self,
        *,
        reviewer_outputs: List[Dict[str, Any]],
        changed_lines: Dict[str, List[int]],
        risk_tier: str,
    ) -> JudgeResult:
        accepted_by_key: Dict[str, Dict[str, Any]] = {}
        rejected: List[Dict[str, Any]] = []
        for output in reviewer_outputs:
            review_run_id = output.get("review_run_id")
            reviewer = output.get("reviewer")
            for candidate in output.get("findings") or []:
                reason = self._rejection_reason(candidate, changed_lines)
                if reason:
                    rejected.append({"source_review_run_id": review_run_id, "reviewer": reviewer, "reason": reason, "candidate": candidate})
                    continue
                normalized = self._normalize_candidate(candidate, review_run_id, reviewer)
                key = self._dedupe_key(normalized)
                existing = accepted_by_key.get(key)
                if existing is None:
                    accepted_by_key[key] = normalized
                    continue
                winner, loser = self._pick_stronger(existing, normalized)
                accepted_by_key[key] = winner
                rejected.append(
                    {
                        "source_review_run_id": loser.get("source_review_run_id"),
                        "reviewer": loser.get("source_reviewer"),
                        "reason": "duplicate_merged",
                        "candidate": candidate,
                    }
                )
        accepted = list(accepted_by_key.values())
        rationale = "all required reviewers produced no accepted findings" if not accepted and risk_tier != "full" else ""
        return JudgeResult(accepted_findings=accepted, rejected_candidates=rejected, accepted_empty_rationale=rationale)

    @staticmethod
    def _rejection_reason(candidate: Dict[str, Any], changed_lines: Dict[str, List[int]]) -> str:
        if not candidate.get("title") or not candidate.get("detail") or not candidate.get("file"):
            return "schema_incomplete"
        confidence = float(candidate.get("confidence", 1.0) if candidate.get("confidence") is not None else 1.0)
        if confidence < 0.3:
            return "low_confidence"
        file_path = str(candidate.get("file"))
        line = candidate.get("line")
        if line is not None and file_path in changed_lines and int(line) not in set(changed_lines[file_path]):
            return "needs_evidence"
        lowered = file_path.lower()
        if "/test/" in lowered or lowered.endswith("_test.py") or lowered.endswith("test.py"):
            return "test_only"
        return ""

    @staticmethod
    def _normalize_candidate(candidate: Dict[str, Any], review_run_id: Any, reviewer: Any) -> Dict[str, Any]:
        severity = normalize_severity(str(candidate.get("severity") or "medium")).value
        file_path = str(candidate.get("file"))
        line = candidate.get("line")
        normalized = {
            "file_path": file_path,
            "line": int(line) if line is not None else None,
            "severity": severity,
            "title": str(candidate.get("title")),
            "detail": str(candidate.get("detail")),
            "suggestion": candidate.get("suggestion"),
            "confidence": float(candidate.get("confidence", 1.0) if candidate.get("confidence") is not None else 1.0),
            "source_review_run_id": review_run_id,
            "source_reviewer": reviewer,
        }
        normalized["finding_id"] = stable_finding_id(normalized)
        return normalized

    @staticmethod
    def _dedupe_key(finding: Dict[str, Any]) -> str:
        return f"{finding['file_path']}:{finding.get('line')}:{str(finding['title']).strip().lower()}"

    @staticmethod
    def _pick_stronger(left: Dict[str, Any], right: Dict[str, Any]) -> tuple:
        left_rank = (_SEVERITY_RANK.get(left["severity"], 0), float(left.get("confidence") or 0))
        right_rank = (_SEVERITY_RANK.get(right["severity"], 0), float(right.get("confidence") or 0))
        return (right, left) if right_rank > left_rank else (left, right)


def stable_finding_id(finding: Dict[str, Any]) -> str:
    raw = "|".join(
        [
            str(finding.get("file_path") or ""),
            str(finding.get("line") or ""),
            str(finding.get("title") or "").strip().lower(),
            str(finding.get("detail") or "").strip().lower(),
        ]
    )
    return "fnd_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

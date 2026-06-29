"""Deterministic context preparation for CR v2 review tasks."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from cr_agent.config import Settings
from cr_agent.review_v2.artifacts import ArtifactStore
from cr_agent.review_v2.review_rules import classify_review_file, match_review_rules
from cr_agent.review_v2.risk import classify_risk, plan_reviewers


_FEATURE_KEY_RE = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b", re.IGNORECASE)
_FEATURE_SPEC_EXTENSIONS = {".md", ".txt", ".rst", ".json", ".yaml", ".yml"}
_FEATURE_SPEC_INCLUDE_RE = re.compile(
    r"(spec|design|requirement|requirements|prd|proposal|story|task|ticket|release[-_]?approval|需求|方案|设计|产品)",
    re.IGNORECASE,
)
_FEATURE_SPEC_NOISE_RE = re.compile(r"(evidence|report|coverage|snapshot|result|log|cache)", re.IGNORECASE)
_FEATURE_SPEC_DOC_PREFIXES = ("doc/", "docs/", "design/", "designs/", "spec/", "specs/", "requirements/")
_MAX_FEATURE_SPEC_REFERENCES = 5
_MAX_FEATURE_SPEC_BYTES = 12_000


@dataclass(frozen=True)
class ChangedFileSummary:
    path: str
    changed_lines: int
    production: bool
    ignored: bool
    test: bool = False
    bloat: bool = False
    skip_reason: str = ""


@dataclass(frozen=True)
class PreparedContext:
    task_id: str
    context_dir: Path
    diff_range: str
    changed_files: List[str]
    changed_lines: Dict[str, List[int]]
    file_summaries: List[ChangedFileSummary]
    truncated: bool
    risk_tier: str


class ContextBuilder:
    def __init__(self, settings: Settings):
        self.settings = settings

    def prepare(
        self,
        *,
        task_id: str,
        repo_path: Path,
        request: Dict[str, Any],
        diff_range: str,
    ) -> PreparedContext:
        context_dir = self.settings.review_v2_audit_dir / task_id / "context"
        store = ArtifactStore(context_dir)
        full_diff_text = self._git(repo_path, "diff", "--unified=80", "--diff-filter=ACMR", diff_range)
        full_diff_meta = store.write_text(
            "diff_full.patch",
            full_diff_text,
            max_bytes=self.settings.review_v2_context_max_bytes,
        )
        all_changed_files = [
            line.strip()
            for line in self._git(repo_path, "diff", "--name-only", "--diff-filter=ACMR", diff_range).splitlines()
            if line.strip()
        ]
        all_changed_lines = self._changed_lines(full_diff_text)
        summaries: List[ChangedFileSummary] = []
        for path in all_changed_files:
            policy = classify_review_file(path)
            ignored = self._is_ignored_file(path) or policy.skipped
            skip_reason = self._skip_reason(path, policy=policy, ignored=ignored)
            summaries.append(
                ChangedFileSummary(
                    path=path,
                    changed_lines=len(all_changed_lines.get(path, [])),
                    production=not ignored,
                    ignored=ignored,
                    test=policy.test,
                    bloat=policy.bloat,
                    skip_reason=skip_reason,
                )
            )
        changed_files = [item.path for item in summaries if item.production]
        feature_spec_references = self._feature_spec_references(
            repo_path=repo_path,
            request=request,
            all_changed_files=all_changed_files,
        )
        review_rule_matches = match_review_rules(changed_files)
        diff_text = (
            self._git(repo_path, "diff", "--unified=80", "--diff-filter=ACMR", diff_range, "--", *changed_files)
            if changed_files
            else ""
        )
        diff_meta = store.write_text("diff.patch", diff_text, max_bytes=self.settings.review_v2_context_max_bytes)
        changed_lines = self._changed_lines(diff_text)
        risk = classify_risk(
            changed_files=summaries,
            diff_bytes=diff_meta["bytes"],
            truncated=bool(diff_meta["truncated"]),
        )
        reviewer_plan = plan_reviewers(risk)
        store.write_json("changed_files.json", changed_files)
        store.write_json("all_changed_files.json", all_changed_files)
        store.write_json("changed_lines.json", changed_lines)
        store.write_json("all_changed_lines.json", all_changed_lines)
        store.write_json("file_summaries.json", [asdict(item) for item in summaries])
        store.write_json("matched_review_rules.json", [item.to_dict() for item in review_rule_matches])
        store.write_json("feature_spec_references.json", feature_spec_references)
        store.write_json("ci_request.json", request)
        store.write_json(
            "llm_context.json",
            {
                "task_id": task_id,
                "diff_range": diff_range,
                "changed_files": changed_files,
                "all_changed_files": all_changed_files,
                "excluded_files": [
                    {"path": item.path, "reason": item.skip_reason}
                    for item in summaries
                    if not item.production
                ],
                "changed_lines": changed_lines,
                "matched_review_rules": [item.to_dict() for item in review_rule_matches],
                "feature_spec_references": feature_spec_references,
                "diff": diff_meta,
                "full_diff": full_diff_meta,
            },
        )
        store.write_json(
            "prompt_references.json",
            {
                "summary": "prompt references",
                "asset_root": "cr_agent.review_v2.templates",
                "references": [
                    "references/review.md",
                    "references/personas/<reviewer>.md",
                    "references/static-analysis-checklist.md",
                    "references/false_positive_patterns.md",
                    "references/review-practices.md",
                    "references/rules/<matched>.md",
                    "context/feature_spec_references.json",
                ],
            },
        )
        store.write_json(
            "risk.json",
            {
                "tier": risk.tier,
                "reasons": risk.reasons,
                "specialists": risk.specialists,
                "context_too_large": risk.context_too_large,
            },
        )
        store.write_json(
            "reviewer_plan.json",
            [
                {"reviewer": item.reviewer, "required": item.required, "reason": item.reason}
                for item in reviewer_plan
            ],
        )
        store.write_json(
            "coverage_plan.json",
            {
                "all_changed_files": all_changed_files,
                "changed_files": changed_files,
                "skipped_files": [
                    {"path": item.path, "reason": item.skip_reason}
                    for item in summaries
                    if not item.production
                ],
                "file_summaries": [asdict(item) for item in summaries],
                "matched_review_rules": [item.to_dict() for item in review_rule_matches],
                "feature_spec_references": [
                    {
                        "path": item["path"],
                        "reason": item["reason"],
                        "matched_keys": item["matched_keys"],
                        "truncated": item["truncated"],
                    }
                    for item in feature_spec_references
                ],
                "risk": {
                    "tier": risk.tier,
                    "reasons": risk.reasons,
                    "specialists": risk.specialists,
                    "context_too_large": risk.context_too_large,
                },
                "reviewer_plan": [
                    {"reviewer": item.reviewer, "required": item.required, "reason": item.reason}
                    for item in reviewer_plan
                ],
            },
        )
        return PreparedContext(
            task_id=task_id,
            context_dir=context_dir,
            diff_range=diff_range,
            changed_files=changed_files,
            changed_lines=changed_lines,
            file_summaries=summaries,
            truncated=bool(diff_meta["truncated"]),
            risk_tier=risk.tier,
        )

    @staticmethod
    def _git(repo_path: Path, *args: str) -> str:
        return subprocess.check_output(["git", "-C", str(repo_path), *args], text=True)

    @staticmethod
    def _changed_lines(diff_text: str) -> Dict[str, List[int]]:
        current_file: Optional[str] = None
        current_line: Optional[int] = None
        result: Dict[str, List[int]] = {}
        for raw_line in diff_text.splitlines():
            if raw_line.startswith("+++ b/"):
                current_file = raw_line.removeprefix("+++ b/")
                result.setdefault(current_file, [])
                continue
            if raw_line.startswith("@@"):
                match = re.search(r"\+(\d+)(?:,(\d+))?", raw_line)
                current_line = int(match.group(1)) if match else None
                continue
            if current_file is None or current_line is None:
                continue
            if raw_line.startswith("+") and not raw_line.startswith("+++"):
                result.setdefault(current_file, []).append(current_line)
                current_line += 1
            elif raw_line.startswith("-") and not raw_line.startswith("---"):
                continue
            else:
                current_line += 1
        return result

    @staticmethod
    def _is_ignored_file(path: str) -> bool:
        lowered = path.lower()
        return lowered in {"readme.md"} or lowered.endswith(".md") or lowered.startswith("docs/")

    @staticmethod
    def _skip_reason(path: str, *, policy: Any, ignored: bool) -> str:
        if policy.skip_reason:
            return str(policy.skip_reason)
        if ignored:
            return "ignored file"
        return ""

    def _feature_spec_references(
        self,
        *,
        repo_path: Path,
        request: Dict[str, Any],
        all_changed_files: List[str],
    ) -> List[Dict[str, Any]]:
        feature_keys = self._feature_keys(request, all_changed_files)
        changed_paths = set(all_changed_files)
        candidates: List[tuple[int, str, str, List[str]]] = []
        for path in self._tracked_files(repo_path):
            candidate = self._feature_spec_candidate(path, changed_paths=changed_paths, feature_keys=feature_keys)
            if candidate is None:
                continue
            score, reason, matched_keys = candidate
            candidates.append((score, path, reason, matched_keys))

        references: List[Dict[str, Any]] = []
        seen: Set[str] = set()
        for _, path, reason, matched_keys in sorted(candidates):
            if path in seen:
                continue
            seen.add(path)
            ref = self._read_feature_spec_reference(repo_path, path, reason=reason, matched_keys=matched_keys)
            if ref is not None:
                references.append(ref)
            if len(references) >= _MAX_FEATURE_SPEC_REFERENCES:
                break
        return references

    @classmethod
    def _feature_keys(cls, request: Dict[str, Any], paths: List[str]) -> Set[str]:
        values: List[str] = list(paths)

        def collect(value: Any) -> None:
            if isinstance(value, str):
                values.append(value)
            elif isinstance(value, dict):
                for item in value.values():
                    collect(item)
            elif isinstance(value, list):
                for item in value:
                    collect(item)

        collect(request)
        keys: Set[str] = set()
        for value in values:
            keys.update(match.group(0).upper() for match in _FEATURE_KEY_RE.finditer(value))
        return keys

    @staticmethod
    def _tracked_files(repo_path: Path) -> List[str]:
        try:
            output = ContextBuilder._git(repo_path, "ls-files")
        except subprocess.CalledProcessError:
            return []
        return [line.strip() for line in output.splitlines() if line.strip()]

    @staticmethod
    def _feature_spec_candidate(
        path: str,
        *,
        changed_paths: Set[str],
        feature_keys: Set[str],
    ) -> Optional[tuple[int, str, List[str]]]:
        lowered = path.lower()
        if Path(path).suffix.lower() not in _FEATURE_SPEC_EXTENSIONS:
            return None
        if _FEATURE_SPEC_NOISE_RE.search(lowered) and not _FEATURE_SPEC_INCLUDE_RE.search(lowered):
            return None

        spec_like = bool(_FEATURE_SPEC_INCLUDE_RE.search(lowered))
        changed = path in changed_paths
        doc_path = lowered.startswith(_FEATURE_SPEC_DOC_PREFIXES)
        matched_keys = sorted(key for key in feature_keys if key in path.upper())

        if changed and spec_like:
            return 0, "changed_spec_file", matched_keys
        if matched_keys and spec_like:
            return 1, "feature_key_spec_match", matched_keys
        if matched_keys and doc_path:
            return 2, "feature_key_doc_match", matched_keys
        return None

    @staticmethod
    def _read_feature_spec_reference(
        repo_path: Path,
        path: str,
        *,
        reason: str,
        matched_keys: List[str],
    ) -> Optional[Dict[str, Any]]:
        target = repo_path / path
        if not target.is_file():
            return None
        size = target.stat().st_size
        with target.open("rb") as handle:
            display_raw = handle.read(_MAX_FEATURE_SPEC_BYTES + 1)
        truncated = size > _MAX_FEATURE_SPEC_BYTES
        if truncated:
            display_raw = display_raw[:_MAX_FEATURE_SPEC_BYTES]
        text = display_raw.decode("utf-8", errors="replace")
        if truncated:
            text += "\n\n[CR_V2_FEATURE_SPEC_TRUNCATED]"
        return {
            "path": path,
            "reason": reason,
            "matched_keys": matched_keys,
            "bytes": size,
            "truncated": truncated,
            "text": text,
        }

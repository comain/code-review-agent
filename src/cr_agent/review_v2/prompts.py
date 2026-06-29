"""Prompt rendering for CR v2 reviewers and judge."""

from __future__ import annotations

from dataclasses import dataclass
import json
from importlib import resources
from pathlib import Path
from typing import Any, Dict, Tuple

from jinja2 import Template

from cr_agent.config import Settings
from cr_agent.review_v2.risk import ReviewerPlanItem, RiskResult
from cr_agent.review_v2.reviewer_profiles import reviewer_profile


@dataclass(frozen=True)
class PromptArtifact:
    path: Path
    inputs_path: Path


class PromptRenderer:
    def __init__(self, settings: Settings):
        self.settings = settings

    def render_reviewer_prompt(
        self,
        *,
        task_id: str,
        context_dir: Path,
        risk: RiskResult,
        reviewer: ReviewerPlanItem,
    ) -> PromptArtifact:
        prompt_dir = context_dir.parent / "reviewers" / reviewer.reviewer
        prompt_dir.mkdir(parents=True, exist_ok=True)
        diff_text, diff_truncated = self._read_text_limited(
            context_dir / "diff.patch",
            max_bytes=self._inline_diff_max_bytes(),
        )
        profile = reviewer_profile(reviewer.reviewer)
        matched_review_rules = self._read_json(context_dir / "matched_review_rules.json", default=[])
        feature_spec_references = self._read_json(context_dir / "feature_spec_references.json", default=[])
        prompt_references = self._prompt_reference_texts(profile.persona_file, matched_review_rules)
        inputs = {
            "task_id": task_id,
            "reviewer": reviewer.reviewer,
            "required": reviewer.required,
            "reviewer_reason": reviewer.reason,
            "reviewer_profile": profile.to_dict(),
            "reviewer_profile_json": json.dumps(profile.to_dict(), ensure_ascii=False, indent=2),
            "prompt_asset_root": "cr_agent.review_v2.templates",
            "prompt_references": prompt_references,
            "prompt_references_json": json.dumps(prompt_references, ensure_ascii=False, indent=2),
            "matched_review_rules": matched_review_rules,
            "matched_review_rules_json": json.dumps(matched_review_rules, ensure_ascii=False, indent=2),
            "feature_spec_references": feature_spec_references,
            "feature_spec_references_json": json.dumps(feature_spec_references, ensure_ascii=False, indent=2),
            "risk_tier": risk.tier,
            "risk_reasons": risk.reasons,
            "risk_reasons_json": json.dumps(risk.reasons, ensure_ascii=False, indent=2),
            "context_dir": str(context_dir),
            "diff_path": str(context_dir / "diff.patch"),
            "diff_text": diff_text,
            "diff_truncated": diff_truncated,
            "changed_files_json": self._read_json_pretty(context_dir / "changed_files.json"),
            "changed_lines_json": self._read_json_pretty(context_dir / "changed_lines.json"),
            "ci_request_json": self._read_json_pretty(context_dir / "ci_request.json"),
        }
        inputs_path = prompt_dir / "prompt_inputs.json"
        inputs_path.write_text(json.dumps(inputs, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        prompt_path = prompt_dir / "prompt.md"
        prompt_path.write_text(self._render_template("reviewer.md.j2", inputs), encoding="utf-8")
        return PromptArtifact(path=prompt_path, inputs_path=inputs_path)

    def render_judge_prompt(
        self,
        *,
        task_id: str,
        context_dir: Path,
        risk: RiskResult,
        reviewer_outputs: list[dict[str, Any]],
    ) -> PromptArtifact:
        prompt_dir = context_dir.parent / "judge"
        prompt_dir.mkdir(parents=True, exist_ok=True)
        feature_spec_references = self._read_json(context_dir / "feature_spec_references.json", default=[])
        inputs = {
            "task_id": task_id,
            "risk_tier": risk.tier,
            "risk_reasons": risk.reasons,
            "risk_reasons_json": json.dumps(risk.reasons, ensure_ascii=False, indent=2),
            "context_dir": str(context_dir),
            "diff_path": str(context_dir / "diff.patch"),
            "changed_files_json": self._read_json_pretty(context_dir / "changed_files.json"),
            "changed_lines_json": self._read_json_pretty(context_dir / "changed_lines.json"),
            "ci_request_json": self._read_json_pretty(context_dir / "ci_request.json"),
            "feature_spec_references": feature_spec_references,
            "feature_spec_references_json": json.dumps(feature_spec_references, ensure_ascii=False, indent=2),
            "reviewer_outputs": reviewer_outputs,
            "reviewer_outputs_json": json.dumps(reviewer_outputs, ensure_ascii=False, indent=2),
        }
        inputs_path = prompt_dir / "prompt_inputs.json"
        inputs_path.write_text(json.dumps(inputs, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        prompt_path = prompt_dir / "prompt.md"
        prompt_path.write_text(self._render_template("judge.md.j2", inputs), encoding="utf-8")
        return PromptArtifact(path=prompt_path, inputs_path=inputs_path)

    @staticmethod
    def _render_template(name: str, values: Dict[str, Any]) -> str:
        template_text = (resources.files("cr_agent.review_v2.templates") / name).read_text(encoding="utf-8")
        return Template(template_text).render(**values)

    def _inline_diff_max_bytes(self) -> int:
        return max(20_000, min(int(self.settings.review_v2_prompt_max_bytes), 500_000))

    def _prompt_reference_texts(self, persona_file: str, matched_review_rules: list[dict[str, Any]]) -> list[dict[str, str]]:
        reference_names = [
            "review.md",
            f"personas/{persona_file}",
            "static-analysis-checklist.md",
            "false_positive_patterns.md",
            "review-practices.md",
        ]
        for item in matched_review_rules:
            for reference in item.get("references") or []:
                name = reference.removeprefix("references/")
                if name not in reference_names:
                    reference_names.append(name)
        package = resources.files("cr_agent.review_v2.templates").joinpath("references")
        return [
            {
                "name": name,
                "text": self._read_reference_text(package, name),
            }
            for name in reference_names
        ]

    def _read_reference_text(self, package: Any, name: str) -> str:
        if name == "false_positive_patterns.md" and self.settings.review_v2_feedback_pattern_output_path:
            path = self.settings.review_v2_feedback_pattern_output_path
            if path.exists():
                return path.read_text(encoding="utf-8")
        return package.joinpath(name).read_text(encoding="utf-8")

    @staticmethod
    def _read_text(path: Path) -> str:
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")

    @classmethod
    def _read_text_limited(cls, path: Path, *, max_bytes: int) -> Tuple[str, bool]:
        if not path.exists():
            return "", False
        raw = path.read_bytes()
        truncated = len(raw) > max_bytes
        if truncated:
            raw = raw[:max_bytes]
        text = raw.decode("utf-8", errors="replace")
        if truncated:
            text += "\n\n[CR_V2_INLINE_DIFF_TRUNCATED]"
        return text, truncated

    @classmethod
    def _read_json_pretty(cls, path: Path) -> str:
        text = cls._read_text(path)
        if not text:
            return "null"
        try:
            return json.dumps(json.loads(text), ensure_ascii=False, indent=2, sort_keys=True)
        except json.JSONDecodeError:
            return text

    @classmethod
    def _read_json(cls, path: Path, *, default: Any) -> Any:
        text = cls._read_text(path)
        if not text:
            return default
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return default

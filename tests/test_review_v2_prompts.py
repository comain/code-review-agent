import json
from pathlib import Path

from cr_agent.config import Settings
from cr_agent.review_v2.prompts import PromptRenderer
from cr_agent.review_v2.risk import ReviewerPlanItem, RiskResult


def test_prompt_renderer_writes_prompt_inputs_and_prompt(tmp_path: Path) -> None:
    context_dir = tmp_path / "audit" / "task1" / "context"
    context_dir.mkdir(parents=True)
    (context_dir / "diff.patch").write_text("diff --git a/src/app.py b/src/app.py\n", encoding="utf-8")
    (context_dir / "changed_files.json").write_text(json.dumps(["src/app.py"]), encoding="utf-8")
    (context_dir / "changed_lines.json").write_text(json.dumps({"src/app.py": [1]}), encoding="utf-8")
    (context_dir / "matched_review_rules.json").write_text(
        json.dumps(
            [
                {
                    "path": "src/app.py",
                    "rule_names": ["default"],
                    "references": ["references/rules/default.md"],
                }
            ]
        ),
        encoding="utf-8",
    )
    (context_dir / "feature_spec_references.json").write_text(
        json.dumps(
            [
                {
                    "path": "docs/spec-DEMO-123.md",
                    "reason": "feature_key_spec_match",
                    "matched_keys": ["DEMO-123"],
                    "bytes": 54,
                    "truncated": False,
                    "text": "The endpoint must reject empty names.",
                }
            ]
        ),
        encoding="utf-8",
    )
    (context_dir / "ci_request.json").write_text(json.dumps({"app_name": "demo"}), encoding="utf-8")
    settings = Settings(base_dir=tmp_path / "runtime")
    renderer = PromptRenderer(settings)

    prompt = renderer.render_reviewer_prompt(
        task_id="task1",
        context_dir=context_dir,
        risk=RiskResult(tier="light", reasons=["small production diff"], specialists=[]),
        reviewer=ReviewerPlanItem(reviewer="correctness_light", required=True, reason="light production diff"),
    )

    prompt_dir = context_dir.parent / "reviewers" / "correctness_light"
    inputs = json.loads((prompt_dir / "prompt_inputs.json").read_text(encoding="utf-8"))
    assert inputs["reviewer"] == "correctness_light"
    assert inputs["prompt_asset_root"] == "cr_agent.review_v2.templates"
    assert prompt.path == prompt_dir / "prompt.md"
    prompt_text = prompt.path.read_text(encoding="utf-8")
    assert "correctness_light" in prompt_text
    assert "review.md" in prompt_text
    assert "inspect dependencies" in prompt_text
    assert "Tool policy is reviewer-specific" in prompt_text
    assert "Use task/delegation tools only when needed" in prompt_text
    assert "Do not edit source files" in prompt_text
    assert "Return exactly one JSON object" in prompt_text
    assert "All user-facing natural language values must be Chinese" in prompt_text
    assert "简短中文问题标题" in prompt_text
    assert "Prompt reference files" in prompt_text
    assert "personas/code-reviewer.md" in prompt_text
    assert "Review the tests first because they reveal intent and coverage" in prompt_text
    assert "Evaluate every change across these five dimensions" in prompt_text
    assert "CR v2 Review Prompt Reference" in prompt_text
    assert "static-analysis-checklist.md" in prompt_text
    assert "review-practices.md" in prompt_text
    assert "Callback And Notification Replay" in prompt_text
    assert "Dependency Discipline" in prompt_text
    assert "模型反馈归档" in prompt_text
    assert "Matched review rule overlays" in prompt_text
    assert "Default Review Rule Overlay" in prompt_text
    assert "Feature spec/reference files" in prompt_text
    assert "original feature intent" in prompt_text
    assert "docs/spec-DEMO-123.md" in prompt_text
    assert "The endpoint must reject empty names." in prompt_text
    assert "diff --git a/src/app.py b/src/app.py" in prompt_text
    assert '"src/app.py": [' in prompt_text
    assert "Do not inspect the filesystem or repository unless" not in prompt_text
    assert "Do not call subagents" not in prompt_text
    assert "Do not call task/delegation tools" not in prompt_text
    assert inputs["diff_truncated"] is False
    assert "Dependency download or build metadata inspection is allowed" not in prompt_text
    reference_names = [reference["name"] for reference in inputs["prompt_references"]]
    assert reference_names == [
        "review.md",
        "personas/code-reviewer.md",
        "static-analysis-checklist.md",
        "false_positive_patterns.md",
        "review-practices.md",
        "rules/default.md",
    ]
    assert "CR v2 Review Prompt Reference" in inputs["prompt_references"][0]["text"]
    assert "审查流程" in inputs["prompt_references"][0]["text"]
    assert "first-principles check" in inputs["prompt_references"][0]["text"]
    assert "Review the tests first because they reveal intent and coverage" in inputs["prompt_references"][1]["text"]
    assert "False-Positive Controls" in inputs["prompt_references"][2]["text"]
    assert inputs["matched_review_rules"][0]["rule_names"] == ["default"]
    assert inputs["feature_spec_references"][0]["path"] == "docs/spec-DEMO-123.md"


def test_prompt_renderer_marks_truncated_inline_diff(tmp_path: Path) -> None:
    context_dir = tmp_path / "audit" / "task1" / "context"
    context_dir.mkdir(parents=True)
    (context_dir / "diff.patch").write_text("x" * 30_000, encoding="utf-8")
    (context_dir / "changed_files.json").write_text("[]", encoding="utf-8")
    (context_dir / "changed_lines.json").write_text("{}", encoding="utf-8")
    (context_dir / "ci_request.json").write_text("{}", encoding="utf-8")
    settings = Settings(base_dir=tmp_path / "runtime", review_v2_prompt_max_bytes=10_000)
    renderer = PromptRenderer(settings)

    prompt = renderer.render_reviewer_prompt(
        task_id="task1",
        context_dir=context_dir,
        risk=RiskResult(tier="full", reasons=["large"], specialists=[]),
        reviewer=ReviewerPlanItem(reviewer="correctness", required=True, reason="full production diff"),
    )

    prompt_text = prompt.path.read_text(encoding="utf-8")
    inputs = json.loads(prompt.inputs_path.read_text(encoding="utf-8"))
    assert "[CR_V2_INLINE_DIFF_TRUNCATED]" in prompt_text
    assert inputs["diff_truncated"] is True


def test_prompt_renderer_uses_feedback_pattern_output_override(tmp_path: Path) -> None:
    context_dir = tmp_path / "audit" / "task1" / "context"
    context_dir.mkdir(parents=True)
    (context_dir / "diff.patch").write_text("diff --git a/src/app.py b/src/app.py\n", encoding="utf-8")
    (context_dir / "changed_files.json").write_text(json.dumps(["src/app.py"]), encoding="utf-8")
    (context_dir / "changed_lines.json").write_text(json.dumps({"src/app.py": [1]}), encoding="utf-8")
    (context_dir / "matched_review_rules.json").write_text("[]", encoding="utf-8")
    (context_dir / "ci_request.json").write_text(json.dumps({"app_name": "demo"}), encoding="utf-8")
    patterns_path = tmp_path / "runtime" / "feedback_patterns.md"
    patterns_path.parent.mkdir(parents=True)
    patterns_path.write_text("# 模型反馈归档\n\ncustom feedback pattern\n", encoding="utf-8")
    settings = Settings(base_dir=tmp_path / "runtime", review_v2_feedback_pattern_output_path=patterns_path)

    prompt = PromptRenderer(settings).render_reviewer_prompt(
        task_id="task1",
        context_dir=context_dir,
        risk=RiskResult(tier="light", reasons=["small production diff"], specialists=[]),
        reviewer=ReviewerPlanItem(reviewer="correctness_light", required=True, reason="light production diff"),
    )

    prompt_text = prompt.path.read_text(encoding="utf-8")
    inputs = json.loads(prompt.inputs_path.read_text(encoding="utf-8"))
    assert "custom feedback pattern" in prompt_text
    assert next(
        reference for reference in inputs["prompt_references"] if reference["name"] == "false_positive_patterns.md"
    )["text"] == "# 模型反馈归档\n\ncustom feedback pattern\n"


def test_prompt_renderer_adds_language_rule_references(tmp_path: Path) -> None:
    context_dir = tmp_path / "audit" / "task1" / "context"
    context_dir.mkdir(parents=True)
    (context_dir / "diff.patch").write_text(
        "diff --git a/provider/src/main/java/com/demo/Foo.java b/provider/src/main/java/com/demo/Foo.java\n",
        encoding="utf-8",
    )
    (context_dir / "changed_files.json").write_text(
        json.dumps(["provider/src/main/java/com/demo/Foo.java"]),
        encoding="utf-8",
    )
    (context_dir / "changed_lines.json").write_text(
        json.dumps({"provider/src/main/java/com/demo/Foo.java": [4]}),
        encoding="utf-8",
    )
    (context_dir / "matched_review_rules.json").write_text(
        json.dumps(
            [
                {
                    "path": "provider/src/main/java/com/demo/Foo.java",
                    "rule_names": ["default", "java"],
                    "references": ["references/rules/default.md", "references/rules/java.md"],
                }
            ]
        ),
        encoding="utf-8",
    )
    (context_dir / "ci_request.json").write_text(json.dumps({"app_name": "demo"}), encoding="utf-8")
    renderer = PromptRenderer(Settings(base_dir=tmp_path / "runtime"))

    prompt = renderer.render_reviewer_prompt(
        task_id="task1",
        context_dir=context_dir,
        risk=RiskResult(tier="light", reasons=["small production diff"], specialists=[]),
        reviewer=ReviewerPlanItem(reviewer="correctness_light", required=True, reason="light production diff"),
    )

    inputs = json.loads(prompt.inputs_path.read_text(encoding="utf-8"))
    prompt_text = prompt.path.read_text(encoding="utf-8")
    reference_names = [reference["name"] for reference in inputs["prompt_references"]]
    assert "rules/java.md" in reference_names
    assert "Java Review Rule Overlay" in prompt_text
    assert inputs["matched_review_rules"][0]["rule_names"] == ["default", "java"]


def test_prompt_renderer_adds_dedicated_reviewer_profile(tmp_path: Path) -> None:
    context_dir = tmp_path / "audit" / "task1" / "context"
    context_dir.mkdir(parents=True)
    (context_dir / "diff.patch").write_text("diff --git a/src/auth.py b/src/auth.py\n", encoding="utf-8")
    (context_dir / "changed_files.json").write_text(json.dumps(["src/auth.py"]), encoding="utf-8")
    (context_dir / "changed_lines.json").write_text(json.dumps({"src/auth.py": [7]}), encoding="utf-8")
    (context_dir / "ci_request.json").write_text(json.dumps({"app_name": "demo"}), encoding="utf-8")
    settings = Settings(base_dir=tmp_path / "runtime")
    renderer = PromptRenderer(settings)

    prompt = renderer.render_reviewer_prompt(
        task_id="task1",
        context_dir=context_dir,
        risk=RiskResult(tier="full", reasons=["security trigger"], specialists=["security"]),
        reviewer=ReviewerPlanItem(reviewer="security", required=True, reason="security deterministic trigger"),
    )

    inputs = json.loads(prompt.inputs_path.read_text(encoding="utf-8"))
    prompt_text = prompt.path.read_text(encoding="utf-8")
    assert inputs["reviewer_profile"]["source_agent"] == "security-auditor"
    assert "tool_policy" in inputs["reviewer_profile"]
    assert "Task/delegation tools and subagents are allowed" in prompt_text
    assert "Dependency download or build metadata inspection is allowed" in prompt_text
    assert "Prompt reference files" in prompt_text
    assert "CI-specific reviewer profile overlay" in prompt_text
    assert "Trust boundaries and authorization" in prompt_text
    assert "OWASP Top 10 and OWASP LLM Top 10" in prompt_text
    assert "Start from trust boundaries where untrusted data enters" in prompt_text
    assert "Security Auditor" in prompt_text


def test_prompt_renderer_includes_feature_specs_in_judge_prompt(tmp_path: Path) -> None:
    context_dir = tmp_path / "audit" / "task1" / "context"
    context_dir.mkdir(parents=True)
    (context_dir / "changed_files.json").write_text(json.dumps(["src/app.py"]), encoding="utf-8")
    (context_dir / "changed_lines.json").write_text(json.dumps({"src/app.py": [7]}), encoding="utf-8")
    (context_dir / "ci_request.json").write_text(json.dumps({"branch": "feature/DEMO-123"}), encoding="utf-8")
    (context_dir / "feature_spec_references.json").write_text(
        json.dumps(
            [
                {
                    "path": "docs/spec-DEMO-123.md",
                    "reason": "feature_key_spec_match",
                    "matched_keys": ["DEMO-123"],
                    "bytes": 54,
                    "truncated": False,
                    "text": "The endpoint must reject empty names.",
                }
            ]
        ),
        encoding="utf-8",
    )
    renderer = PromptRenderer(Settings(base_dir=tmp_path / "runtime"))

    prompt = renderer.render_judge_prompt(
        task_id="task1",
        context_dir=context_dir,
        risk=RiskResult(tier="standard", reasons=["production diff"], specialists=[]),
        reviewer_outputs=[{"reviewer": "correctness", "findings": []}],
    )

    inputs = json.loads(prompt.inputs_path.read_text(encoding="utf-8"))
    prompt_text = prompt.path.read_text(encoding="utf-8")
    assert inputs["feature_spec_references"][0]["path"] == "docs/spec-DEMO-123.md"
    assert "Feature spec/reference files" in prompt_text
    assert "original feature intent" in prompt_text
    assert "The endpoint must reject empty names." in prompt_text
    assert "All final report text must be Chinese" in prompt_text
    assert "简短中文缺陷标题" in prompt_text

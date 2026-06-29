import json
import subprocess
from pathlib import Path

from cr_agent.config import Settings
from cr_agent.review_v2.context import ContextBuilder


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def _repo_with_change(tmp_path: Path, file_path: str = "src/app.py", content: str = "print('hi')\n") -> Path:
    return _repo_with_changes(tmp_path, {file_path: content})


def _repo_with_changes(tmp_path: Path, changes: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("demo\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    for file_path, content in changes.items():
        target = repo / file_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "change")
    return repo


def test_context_builder_writes_private_artifacts(tmp_path: Path) -> None:
    repo = _repo_with_change(tmp_path)
    settings = Settings(base_dir=tmp_path / "runtime", review_v2_audit_dir=tmp_path / "audit")

    prepared = ContextBuilder(settings).prepare(
        task_id="task1",
        repo_path=repo,
        request={"app_name": "demo", "branch": "feature/a"},
        diff_range="HEAD~1..HEAD",
    )

    context_dir = tmp_path / "audit" / "task1" / "context"
    assert prepared.context_dir == context_dir
    assert (context_dir / "diff.patch").is_file()
    assert json.loads((context_dir / "changed_files.json").read_text()) == ["src/app.py"]
    changed_lines = json.loads((context_dir / "changed_lines.json").read_text())
    assert changed_lines["src/app.py"] == [1]
    assert json.loads((context_dir / "ci_request.json").read_text())["app_name"] == "demo"
    llm_context = json.loads((context_dir / "llm_context.json").read_text())
    assert llm_context["diff_range"] == "HEAD~1..HEAD"
    assert llm_context["matched_review_rules"] == [
        {
            "path": "src/app.py",
            "references": ["references/rules/default.md"],
            "rule_names": ["default"],
        }
    ]
    prompt_references = json.loads((context_dir / "prompt_references.json").read_text())
    assert prompt_references["summary"] == "prompt references"
    assert prompt_references["references"][0] == "references/review.md"
    coverage_plan = json.loads((context_dir / "coverage_plan.json").read_text())
    assert coverage_plan["changed_files"] == ["src/app.py"]
    assert coverage_plan["matched_review_rules"] == llm_context["matched_review_rules"]


def test_context_builder_references_existing_feature_spec_by_ticket_key(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    spec = repo / "docs" / "spec-DEMO-123.md"
    spec.parent.mkdir(parents=True)
    spec.write_text("# DEMO-123 Spec\n\nThe endpoint must reject empty names.\n", encoding="utf-8")
    (repo / "docs" / "evidence-DEMO-123.json").write_text('{"trace": "not a spec"}\n', encoding="utf-8")
    (repo / "README.md").write_text("demo\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    app = repo / "src" / "app.py"
    app.parent.mkdir(parents=True)
    app.write_text("def create(name):\n    return {'name': name}\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "DEMO-123 implementation")
    settings = Settings(base_dir=tmp_path / "runtime", review_v2_audit_dir=tmp_path / "audit")

    prepared = ContextBuilder(settings).prepare(
        task_id="task1",
        repo_path=repo,
        request={"branch": "feature/DEMO-123-create-api"},
        diff_range="HEAD~1..HEAD",
    )

    references = json.loads((prepared.context_dir / "feature_spec_references.json").read_text())
    assert references == [
        {
            "path": "docs/spec-DEMO-123.md",
            "reason": "feature_key_spec_match",
            "matched_keys": ["DEMO-123"],
            "bytes": len("# DEMO-123 Spec\n\nThe endpoint must reject empty names.\n".encode("utf-8")),
            "truncated": False,
            "text": "# DEMO-123 Spec\n\nThe endpoint must reject empty names.\n",
        }
    ]
    llm_context = json.loads((prepared.context_dir / "llm_context.json").read_text())
    assert llm_context["feature_spec_references"][0]["path"] == "docs/spec-DEMO-123.md"
    coverage_plan = json.loads((prepared.context_dir / "coverage_plan.json").read_text())
    assert coverage_plan["feature_spec_references"] == [
        {
            "path": "docs/spec-DEMO-123.md",
            "reason": "feature_key_spec_match",
            "matched_keys": ["DEMO-123"],
            "truncated": False,
        }
    ]


def test_context_builder_truncates_oversized_diff(tmp_path: Path) -> None:
    repo = _repo_with_change(tmp_path, content="x = '" + ("a" * 2000) + "'\n")
    settings = Settings(
        base_dir=tmp_path / "runtime",
        review_v2_audit_dir=tmp_path / "audit",
        review_v2_context_max_bytes=100,
    )

    prepared = ContextBuilder(settings).prepare(
        task_id="task1",
        repo_path=repo,
        request={},
        diff_range="HEAD~1..HEAD",
    )

    assert prepared.truncated is True
    assert (prepared.context_dir / "diff.patch").stat().st_size <= 100


def test_context_builder_excludes_test_and_bloat_files_from_reviewer_context(tmp_path: Path) -> None:
    repo = _repo_with_changes(
        tmp_path,
        {
            "src/app.py": "print('prod')\n",
            "tests/test_app.py": "def test_prod():\n    assert True\n",
            "web/static/app.min.js": "function bundled(){return true;}\n",
        },
    )
    settings = Settings(base_dir=tmp_path / "runtime", review_v2_audit_dir=tmp_path / "audit")

    prepared = ContextBuilder(settings).prepare(
        task_id="task1",
        repo_path=repo,
        request={},
        diff_range="HEAD~1..HEAD",
    )

    context_dir = prepared.context_dir
    assert prepared.changed_files == ["src/app.py"]
    assert json.loads((context_dir / "changed_files.json").read_text()) == ["src/app.py"]
    assert json.loads((context_dir / "all_changed_files.json").read_text()) == [
        "src/app.py",
        "tests/test_app.py",
        "web/static/app.min.js",
    ]
    assert json.loads((context_dir / "changed_lines.json").read_text()) == {"src/app.py": [1]}
    diff_text = (context_dir / "diff.patch").read_text(encoding="utf-8")
    full_diff_text = (context_dir / "diff_full.patch").read_text(encoding="utf-8")
    assert "src/app.py" in diff_text
    assert "tests/test_app.py" not in diff_text
    assert "web/static/app.min.js" not in diff_text
    assert "tests/test_app.py" in full_diff_text
    assert "web/static/app.min.js" in full_diff_text
    summaries = json.loads((context_dir / "file_summaries.json").read_text())
    skipped = {item["path"]: item["skip_reason"] for item in summaries if not item["production"]}
    assert skipped == {
        "tests/test_app.py": "test file",
        "web/static/app.min.js": "bloat file type",
    }


def test_context_builder_skips_test_only_change_without_reviewer_context(tmp_path: Path) -> None:
    repo = _repo_with_change(
        tmp_path,
        file_path="src/test/java/com/demo/AppTest.java",
        content="package com.demo;\nclass AppTest {}\n",
    )
    settings = Settings(base_dir=tmp_path / "runtime", review_v2_audit_dir=tmp_path / "audit")

    prepared = ContextBuilder(settings).prepare(
        task_id="task1",
        repo_path=repo,
        request={},
        diff_range="HEAD~1..HEAD",
    )

    context_dir = prepared.context_dir
    assert prepared.risk_tier == "skipped"
    assert prepared.changed_files == []
    assert prepared.changed_lines == {}
    assert (context_dir / "diff.patch").read_text(encoding="utf-8") == ""
    assert json.loads((context_dir / "reviewer_plan.json").read_text()) == []
    assert json.loads((context_dir / "risk.json").read_text())["reasons"] == ["test files only"]
    llm_context = json.loads((context_dir / "llm_context.json").read_text())
    assert llm_context["excluded_files"] == [
        {"path": "src/test/java/com/demo/AppTest.java", "reason": "test file"}
    ]


def test_context_builder_records_language_rule_overlays(tmp_path: Path) -> None:
    repo = _repo_with_changes(
        tmp_path,
        {
            "provider/src/main/java/com/demo/Foo.java": "package com.demo;\nclass Foo {}\n",
            "provider/src/main/resources/mapper/FooMapper.xml": "<mapper></mapper>\n",
        },
    )
    settings = Settings(base_dir=tmp_path / "runtime", review_v2_audit_dir=tmp_path / "audit")

    prepared = ContextBuilder(settings).prepare(
        task_id="task1",
        repo_path=repo,
        request={},
        diff_range="HEAD~1..HEAD",
    )

    matches = json.loads((prepared.context_dir / "matched_review_rules.json").read_text())
    by_path = {item["path"]: item for item in matches}
    assert by_path["provider/src/main/java/com/demo/Foo.java"]["rule_names"] == ["default", "java"]
    assert by_path["provider/src/main/resources/mapper/FooMapper.xml"]["rule_names"] == [
        "default",
        "mapper_dao_xml",
    ]

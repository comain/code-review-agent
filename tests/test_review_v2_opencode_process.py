import json
import os
from pathlib import Path
import time

from cr_agent.config import Settings
from cr_agent.review_v2.opencode_process import OpenCodeProcessRunner, classify_provider_model_error
from cr_agent.review_v2.opencode_routing import reset_model_health


def _fake_opencode(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "fake-opencode"
    path.write_text(f"#!/bin/sh\ncat <<'EOF'\n{body}\nEOF\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _fake_opencode_capture(tmp_path: Path, capture: Path, body: str) -> Path:
    path = tmp_path / "fake-opencode-capture"
    path.write_text(
        f"#!/bin/sh\npwd > {capture}\nfor arg in \"$@\"; do echo \"$arg\" >> {capture}; done\ncat opencode.json >> {capture}\ncat <<'EOF'\n{body}\nEOF\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _fake_slow_opencode(tmp_path: Path, started: Path) -> Path:
    path = tmp_path / "fake-opencode-slow"
    path.write_text(
        f"#!/bin/sh\nprintf started > {started}\nsleep 20\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _fake_opencode_provider_fallback(tmp_path: Path, capture: Path) -> Path:
    path = tmp_path / "fake-opencode-fallback"
    path.write_text(
        f"""#!/usr/bin/env python3
import json
import pathlib
import sys
import time

capture = pathlib.Path({str(capture)!r})
config = json.loads(pathlib.Path("opencode.json").read_text())
model = config["model"]
with capture.open("a") as fh:
    fh.write(model + "\\n")
if model.startswith("llm-proxy/"):
    print('ERROR service=llm error={{"error":{{"name":"AI_APICallError","cause":{{"code":"ConnectionRefused"}}}}}}', file=sys.stderr, flush=True)
    time.sleep(20)
else:
    print('{{"type":"step_start","sessionID":"ses_fallback","part":{{"type":"step-start"}}}}')
    print('{{"type":"text","sessionID":"ses_fallback","part":{{"type":"text","text":"{{\\\\\\"summary\\\\\\":\\\\\\"ok\\\\\\"}}"}}}}')
    print('{{"type":"step_finish","sessionID":"ses_fallback","cost":0.01,"part":{{"reason":"stop","tokens":{{"input":1,"output":1,"total":2}}}}}}')
""",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _fake_opencode_provider_model_fallback(tmp_path: Path, capture: Path) -> Path:
    path = tmp_path / "fake-opencode-model-fallback"
    path.write_text(
        f"""#!/usr/bin/env python3
import json
import pathlib
import sys
import time

capture = pathlib.Path({str(capture)!r})
config = json.loads(pathlib.Path("opencode.json").read_text())
model = config["model"]
with capture.open("a") as fh:
    fh.write(model + "\\n")
if model == "llm-proxy/claude-fable-5":
    print('ERROR service=llm error={{"error":{{"name":"AI_APICallError","cause":{{"code":"ECONNRESET"}}}}}}', file=sys.stderr, flush=True)
    time.sleep(20)
else:
    print('{{"type":"step_start","sessionID":"ses_model_fallback","part":{{"type":"step-start"}}}}')
    print('{{"type":"text","sessionID":"ses_model_fallback","part":{{"type":"text","text":"{{\\\\\\"summary\\\\\\":\\\\\\"ok\\\\\\"}}"}}}}')
    print('{{"type":"step_finish","sessionID":"ses_model_fallback","cost":0.01,"part":{{"reason":"stop","tokens":{{"input":1,"output":1,"total":2}}}}}}')
""",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _fake_opencode_structured_error_fallback(tmp_path: Path, capture: Path) -> Path:
    path = tmp_path / "fake-opencode-structured-fallback"
    path.write_text(
        f"""#!/usr/bin/env python3
import json
import pathlib

capture = pathlib.Path({str(capture)!r})
config = json.loads(pathlib.Path("opencode.json").read_text())
model = config["model"]
with capture.open("a") as fh:
    fh.write(model + "\\n")
if model.startswith("llm-proxy/"):
    print(json.dumps({{
        "type": "error",
        "sessionID": "ses_structured_failure",
        "error": {{
            "name": "AI_RetryError",
            "errors": [
                {{
                    "name": "AI_APICallError",
                    "cause": {{"code": "ConnectionRefused", "path": "http://openai-compatible.example.com/v1/chat/completions"}}
                }}
            ]
        }}
    }}))
else:
    print('{{"type":"step_start","sessionID":"ses_structured_fallback","part":{{"type":"step-start"}}}}')
    print('{{"type":"text","sessionID":"ses_structured_fallback","part":{{"type":"text","text":"{{\\\\\\"summary\\\\\\":\\\\\\"ok\\\\\\"}}"}}}}')
    print('{{"type":"step_finish","sessionID":"ses_structured_fallback","cost":0.01,"part":{{"reason":"stop","tokens":{{"input":1,"output":1,"total":2}}}}}}')
""",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _fake_opencode_generic_error(tmp_path: Path, capture: Path) -> Path:
    path = tmp_path / "fake-opencode-generic-error"
    path.write_text(
        f"""#!/usr/bin/env python3
import json
import pathlib

capture = pathlib.Path({str(capture)!r})
config = json.loads(pathlib.Path("opencode.json").read_text())
with capture.open("a") as fh:
    fh.write(config["model"] + "\\n")
print(json.dumps({{
    "type": "error",
    "sessionID": "ses_generic_error",
    "error": {{"name": "OpenCodeError", "message": "OpenCode command failed while applying patch"}}
}}))
""",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def test_provider_model_error_classifier_matches_reference_cases() -> None:
    assert classify_provider_model_error({"message": "Model not found: tencent/glm-5"}) == "model_not_found"
    assert classify_provider_model_error({"message": "The requested model is disabled for this account"}) == "model_disabled"
    assert classify_provider_model_error({"message": "Model openai/gpt-5.5 is temporarily unavailable"}) == "model_unavailable"
    assert (
        classify_provider_model_error(
            {
                "name": "APIError",
                "data": {
                    "message": "Authentication Fails, Your api key is invalid",
                    "statusCode": 401,
                },
            }
        )
        == "provider_auth_failed"
    )
    assert classify_provider_model_error({"message": "OpenCode command failed while applying patch"}) is None


def test_process_runner_captures_jsonl_session_tokens_and_raw_log(tmp_path: Path) -> None:
    fake = _fake_opencode(
        tmp_path,
        '\n'.join(
            [
                '{"type":"step_start","sessionID":"ses_proc","part":{"type":"step-start"}}',
                '{"type":"text","sessionID":"ses_proc","part":{"type":"text","text":"{\\"summary\\":\\"ok\\"}"}}',
                '{"type":"step_finish","sessionID":"ses_proc","cost":0.0042,"part":{"reason":"stop","tokens":{"input":10,"output":5,"cache":{"read":2,"write":1},"reasoning":3,"total":21}}}',
            ]
        ),
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("review this", encoding="utf-8")
    settings = Settings(
        opencode_bin=str(fake),
        base_dir=tmp_path / "runtime",
        review_v2_audit_dir=tmp_path / "audit",
    )

    result = OpenCodeProcessRunner(settings).run_turn(
        prompt_file=prompt,
        repo_path=repo,
        model_id="llm-proxy/gpt-5.5",
    )

    assert result.type == "completed"
    assert result.session_id == "ses_proc"
    assert result.model_id == "llm-proxy/gpt-5.5"
    assert result.tokens["total"] == 21
    assert result.cost_usd == 0.0042
    assert result.result == '{"summary":"ok"}'
    assert result.raw_log_path is not None
    raw_log = Path(result.raw_log_path).read_text(encoding="utf-8")
    assert raw_log.count('"kind": "stream_line"') == 3
    assert '"session_id": "ses_proc"' in raw_log
    assert '"model_id": "llm-proxy/gpt-5.5"' in raw_log
    assert '"cost_usd": 0.0042' in raw_log
    assert '"kind": "turn_finish"' in raw_log


def test_process_runner_uses_isolated_config_cwd_and_repo_dir_override(tmp_path: Path, monkeypatch) -> None:
    capture = tmp_path / "args.txt"
    fake = _fake_opencode_capture(
        tmp_path,
        capture,
        '{"type":"step_start","sessionID":"ses_proc","part":{"type":"step-start"}}',
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("review this", encoding="utf-8")
    settings = Settings(
        opencode_bin=str(fake),
        base_dir=tmp_path / "runtime",
        review_v2_audit_dir=tmp_path / "audit",
    )
    monkeypatch.chdir(tmp_path)

    OpenCodeProcessRunner(settings).run_turn(prompt_file=prompt, repo_path=Path("repo"))

    lines = capture.read_text(encoding="utf-8").splitlines()
    cwd = Path(lines[0])
    assert cwd.parent == repo / ".cr_agent" / "opencode" / "configs"
    assert not cwd.exists()
    assert "--dir" not in lines
    assert "--model" not in lines
    assert "--pure" in lines
    assert "--dangerously-skip-permissions" not in lines


def test_process_runner_writes_selected_model_to_opencode_config(tmp_path: Path) -> None:
    capture = tmp_path / "args.txt"
    fake = _fake_opencode_capture(
        tmp_path,
        capture,
        '{"type":"step_start","sessionID":"ses_proc","part":{"type":"step-start"}}',
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("review this", encoding="utf-8")
    settings = Settings(
        opencode_bin=str(fake),
        base_dir=tmp_path / "runtime",
        review_v2_audit_dir=tmp_path / "audit",
        review_v2_opencode_provider_chain="llm-proxy:claude-fable-5,gpt-5.5",
    )

    OpenCodeProcessRunner(settings).run_turn(prompt_file=prompt, repo_path=repo)

    lines = capture.read_text(encoding="utf-8").splitlines()
    assert "--model" not in lines
    config_start = lines.index("{")
    config = json.loads("\n".join(lines[config_start:]))
    assert config["model"] == "llm-proxy/claude-fable-5"


def test_process_runner_falls_back_after_provider_transport_failure(tmp_path: Path) -> None:
    reset_model_health()
    try:
        capture = tmp_path / "models.txt"
        fake = _fake_opencode_provider_fallback(tmp_path, capture)
        repo = tmp_path / "repo"
        repo.mkdir()
        prompt = tmp_path / "prompt.md"
        prompt.write_text("review this", encoding="utf-8")
        settings = Settings(
            opencode_bin=str(fake),
            base_dir=tmp_path / "runtime",
            review_v2_audit_dir=tmp_path / "audit",
            review_v2_opencode_provider_chain="llm-proxy:claude-fable-5,gpt-5.5,gpt-5.4;openai:gpt-5.5",
            task_timeout_seconds=30,
        )
        started_at = time.monotonic()

        result = OpenCodeProcessRunner(settings).run_turn(prompt_file=prompt, repo_path=repo)

        assert time.monotonic() - started_at < 10
        assert capture.read_text(encoding="utf-8").splitlines() == [
            "llm-proxy/claude-fable-5",
            "openai/gpt-5.5",
        ]
        assert result.type == "completed"
        assert result.model_id == "openai/gpt-5.5"
        assert result.session_id == "ses_fallback"
        assert result.tokens["total"] == 2
    finally:
        reset_model_health()


def test_provider_transport_failure_only_skips_provider_for_llm_proxy_connection_refused(tmp_path: Path) -> None:
    reset_model_health()
    try:
        capture = tmp_path / "models.txt"
        fake = _fake_opencode_provider_model_fallback(tmp_path, capture)
        repo = tmp_path / "repo"
        repo.mkdir()
        prompt = tmp_path / "prompt.md"
        prompt.write_text("review this", encoding="utf-8")
        settings = Settings(
            opencode_bin=str(fake),
            base_dir=tmp_path / "runtime",
            review_v2_audit_dir=tmp_path / "audit",
            review_v2_opencode_provider_chain="llm-proxy:claude-fable-5,gpt-5.5;openai:gpt-5.5",
            task_timeout_seconds=30,
        )

        result = OpenCodeProcessRunner(settings).run_turn(prompt_file=prompt, repo_path=repo)

        assert capture.read_text(encoding="utf-8").splitlines() == [
            "llm-proxy/claude-fable-5",
            "llm-proxy/gpt-5.5",
        ]
        assert result.type == "completed"
        assert result.model_id == "llm-proxy/gpt-5.5"
        assert result.session_id == "ses_model_fallback"
    finally:
        reset_model_health()


def test_process_runner_falls_back_after_structured_provider_transport_error(tmp_path: Path) -> None:
    reset_model_health()
    try:
        capture = tmp_path / "models.txt"
        fake = _fake_opencode_structured_error_fallback(tmp_path, capture)
        repo = tmp_path / "repo"
        repo.mkdir()
        prompt = tmp_path / "prompt.md"
        prompt.write_text("review this", encoding="utf-8")
        settings = Settings(
            opencode_bin=str(fake),
            base_dir=tmp_path / "runtime",
            review_v2_audit_dir=tmp_path / "audit",
            review_v2_opencode_provider_chain="llm-proxy:claude-fable-5,gpt-5.5;openai:gpt-5.5",
            task_timeout_seconds=30,
        )

        result = OpenCodeProcessRunner(settings).run_turn(prompt_file=prompt, repo_path=repo)

        assert capture.read_text(encoding="utf-8").splitlines() == [
            "llm-proxy/claude-fable-5",
            "openai/gpt-5.5",
        ]
        assert result.type == "completed"
        assert result.model_id == "openai/gpt-5.5"
        assert result.session_id == "ses_structured_fallback"
    finally:
        reset_model_health()


def test_process_runner_does_not_fallback_on_generic_opencode_error(tmp_path: Path) -> None:
    reset_model_health()
    try:
        capture = tmp_path / "models.txt"
        fake = _fake_opencode_generic_error(tmp_path, capture)
        repo = tmp_path / "repo"
        repo.mkdir()
        prompt = tmp_path / "prompt.md"
        prompt.write_text("review this", encoding="utf-8")
        settings = Settings(
            opencode_bin=str(fake),
            base_dir=tmp_path / "runtime",
            review_v2_audit_dir=tmp_path / "audit",
            review_v2_opencode_provider_chain="llm-proxy:claude-fable-5;openai:gpt-5.5",
            task_timeout_seconds=30,
        )

        result = OpenCodeProcessRunner(settings).run_turn(prompt_file=prompt, repo_path=repo)

        assert capture.read_text(encoding="utf-8").splitlines() == ["llm-proxy/claude-fable-5"]
        assert result.type == "error"
        assert result.model_id == "llm-proxy/claude-fable-5"
        assert result.fallback_eligible is False
        assert result.fallback_reason is None
    finally:
        reset_model_health()


def test_process_runner_writes_large_prompt_file(tmp_path: Path) -> None:
    capture = tmp_path / "args.txt"
    fake = _fake_opencode_capture(
        tmp_path,
        capture,
        '{"type":"step_start","sessionID":"ses_proc","part":{"type":"step-start"}}',
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("x" * 200, encoding="utf-8")
    settings = Settings(
        opencode_bin=str(fake),
        base_dir=tmp_path / "runtime",
        review_v2_audit_dir=tmp_path / "audit",
        review_v2_opencode_prompt_file_threshold_chars=50,
    )

    OpenCodeProcessRunner(settings).run_turn(prompt_file=prompt, repo_path=repo)

    lines = capture.read_text(encoding="utf-8").splitlines()
    file_arg_index = lines.index("--file") + 1
    prompt_file = Path(lines[file_arg_index])
    assert prompt_file.is_file()
    assert prompt_file.read_text(encoding="utf-8") == "x" * 200


def test_process_runner_terminates_when_cancelled(tmp_path: Path) -> None:
    started = tmp_path / "started.txt"
    fake = _fake_slow_opencode(tmp_path, started)
    repo = tmp_path / "repo"
    repo.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("review this", encoding="utf-8")
    settings = Settings(
        opencode_bin=str(fake),
        base_dir=tmp_path / "runtime",
        review_v2_audit_dir=tmp_path / "audit",
        task_timeout_seconds=30,
    )
    started_at = time.monotonic()

    result = OpenCodeProcessRunner(settings).run_turn(
        prompt_file=prompt,
        repo_path=repo,
        is_cancelled=lambda: started.exists(),
    )

    assert result.type == "cancelled"
    assert time.monotonic() - started_at < 5
    assert result.error == {"message": "task cancelled by operator"}


def test_project_opencode_config_is_restored_after_run(tmp_path: Path) -> None:
    fake = _fake_opencode(tmp_path, '{"type":"step_finish","part":{"reason":"stop"}}')
    repo = tmp_path / "repo"
    repo.mkdir()
    original = repo / "opencode.json"
    original.write_text('{"repo":true}', encoding="utf-8")
    prompt = tmp_path / "prompt.md"
    prompt.write_text("review this", encoding="utf-8")
    settings = Settings(
        opencode_bin=str(fake),
        base_dir=tmp_path / "runtime",
        review_v2_audit_dir=tmp_path / "audit",
    )

    OpenCodeProcessRunner(settings).run_turn(prompt_file=prompt, repo_path=repo)

    assert original.read_text(encoding="utf-8") == '{"repo":true}'
    assert not (repo / "opencode.json.cr-v2.bak").exists()


def test_project_opencode_config_is_deleted_when_generated_from_empty_repo(tmp_path: Path) -> None:
    fake = _fake_opencode(tmp_path, '{"type":"step_finish","part":{"reason":"stop"}}')
    repo = tmp_path / "repo"
    repo.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("review this", encoding="utf-8")
    settings = Settings(
        opencode_bin=str(fake),
        base_dir=tmp_path / "runtime",
        review_v2_audit_dir=tmp_path / "audit",
    )

    OpenCodeProcessRunner(settings).run_turn(prompt_file=prompt, repo_path=repo)

    assert not (repo / "opencode.json").exists()
    assert os.listdir(repo) == [".cr_agent"]
    assert list((repo / ".cr_agent" / "opencode" / "configs").iterdir()) == []

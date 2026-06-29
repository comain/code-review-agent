from cr_agent.core.opencode_runner import OpencodeRunner
from cr_agent.config import Settings
from pathlib import Path
import subprocess
import sqlite3


def test_parse_output_extracts_json_block() -> None:
    settings = Settings()
    runner = OpencodeRunner(settings)
    result = runner._parse_output(
        'noise {"summary":"done","pass_check":true,"score":95,"findings":[]} trailing'
    )

    assert result.pass_check is True
    assert result.score == 95


def test_parse_output_supports_opencode_event_stream() -> None:
    settings = Settings()
    runner = OpencodeRunner(settings)
    stdout = """
{"type":"step_start","timestamp":1}
{"type":"text","timestamp":2,"part":{"type":"text","text":"Based on my comprehensive analysis, I can now output the JSON result:\\n\\n{\\"summary\\":\\"done\\",\\"pass_check\\":false,\\"score\\":82,\\"findings\\":[{\\"file\\":\\"biz/src/main/java/com/foo/Bar.java\\",\\"line\\":12,\\"severity\\":\\"high\\",\\"title\\":\\"Bug\\",\\"detail\\":\\"detail\\",\\"suggestion\\":\\"fix\\"}]}"}}
{"type":"step_finish","timestamp":3}
    """.strip()

    result = runner._parse_output(stdout)

    assert result.pass_check is False
    assert result.score == 82
    assert result.findings[0].file == "biz/src/main/java/com/foo/Bar.java"


def test_parse_with_repair_loop_attaches_opencode_session_ids(tmp_path: Path) -> None:
    settings = Settings(llm_format_retry_times=0)
    runner = OpencodeRunner(settings)
    stdout = """
{"type":"step_start","timestamp":1,"sessionID":"ses_test","part":{"type":"step-start","sessionID":"ses_test"}}
{"type":"text","timestamp":2,"sessionID":"ses_test","part":{"type":"text","text":"{\\"summary\\":\\"done\\",\\"pass_check\\":true,\\"score\\":100,\\"findings\\":[]}"}}
{"type":"step_finish","timestamp":3,"sessionID":"ses_test"}
    """.strip()

    result, repair_attempt = runner._parse_with_repair_loop(
        task_id="task1",
        repo_path=tmp_path,
        raw_output=stdout,
    )

    assert repair_attempt == 0
    assert result.raw_output is not None
    assert result.raw_output["_opencode_session_ids"] == ["ses_test"]


def test_parse_output_recovers_final_text_from_opencode_db(monkeypatch) -> None:
    settings = Settings()
    runner = OpencodeRunner(settings)
    stdout = """
{"type":"step_start","timestamp":1,"sessionID":"ses_test","part":{"type":"step-start"}}
{"type":"tool_use","timestamp":2,"sessionID":"ses_test","part":{"type":"tool","state":{"output":"unterminated
    """.strip()

    monkeypatch.setattr(
        OpencodeRunner,
        "_load_text_parts_from_opencode_db",
        staticmethod(lambda session_ids: [
            '{"summary":"done","pass_check":false,"score":75,"findings":[{"file":"a.java","line":9,"severity":"high","title":"问题","detail":"详情","suggestion":"建议"}]}'
            if session_ids == ["ses_test"]
            else ""
        ]),
    )

    result = runner._parse_output(stdout)

    assert result.pass_check is False
    assert result.score == 75
    assert result.findings[0].file == "a.java"


def test_parse_output_recovers_db_text_when_stream_text_is_only_progress(monkeypatch) -> None:
    settings = Settings()
    runner = OpencodeRunner(settings)
    stdout = """
{"type":"step_start","timestamp":1,"sessionID":"ses_test","part":{"type":"step-start"}}
{"type":"text","timestamp":2,"sessionID":"ses_test","part":{"type":"text","text":"我会按给定 guideline 和 diff 范围做静态审查，先并行收集变更上下文。"}}
{"type":"tool_use","timestamp":3,"sessionID":"ses_test","part":{"type":"tool","state":{"output":"unterminated
    """.strip()

    monkeypatch.setattr(
        OpencodeRunner,
        "_load_text_parts_from_opencode_db",
        staticmethod(lambda session_ids: [
            "我会按给定 guideline 和 diff 范围做静态审查，先并行收集变更上下文。",
            '{"summary":"发现 1 个问题","pass_check":true,"score":85,"findings":[{"file":"a.java","line":9,"severity":"medium","title":"问题","detail":"详情","suggestion":"建议"}]}',
        ] if session_ids == ["ses_test"] else []),
    )

    result = runner._parse_output(stdout)

    assert result.score == 85
    assert result.findings[0].severity.value == "medium"
    assert result.findings[0].file == "a.java"


def test_parse_output_rejects_incomplete_event_stream_without_db_text(monkeypatch) -> None:
    settings = Settings()
    runner = OpencodeRunner(settings)
    stdout = """
{"type":"step_start","timestamp":1,"sessionID":"ses_test","part":{"type":"step-start"}}
{"type":"tool_use","timestamp":2,"sessionID":"ses_test","part":{"type":"tool","state":{"output":"unterminated
    """.strip()
    monkeypatch.setattr(OpencodeRunner, "_load_text_parts_from_opencode_db", staticmethod(lambda session_ids: []))

    try:
        runner._parse_output(stdout)
    except RuntimeError as exc:
        assert "incomplete opencode event stream" in str(exc)
    else:
        raise AssertionError("expected incomplete event stream to fail")


def test_parse_output_rejects_opencode_error_event() -> None:
    settings = Settings()
    runner = OpencodeRunner(settings)
    stdout = '{"type":"error","timestamp":1,"error":{"name":"UnknownError","data":{"message":"Model not found: llm-proxy/gpt-5.5."}}}'

    try:
        runner._parse_output(stdout)
    except RuntimeError as exc:
        assert "Model not found: llm-proxy/gpt-5.5" in str(exc)
    else:
        raise AssertionError("expected opencode error event to fail")


def test_load_text_parts_from_opencode_db_reads_assistant_text(monkeypatch, tmp_path: Path) -> None:
    db_dir = tmp_path / ".local" / "share" / "opencode"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "opencode.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("create table message (id text primary key, session_id text, data text)")
        connection.execute("create table part (id text primary key, message_id text, session_id text, time_created integer, data text)")
        connection.execute(
            "insert into message values (?, ?, ?)",
            ("msg_user", "ses_test", '{"role":"user"}'),
        )
        connection.execute(
            "insert into message values (?, ?, ?)",
            ("msg_assistant", "ses_test", '{"role":"assistant"}'),
        )
        connection.execute(
            "insert into part values (?, ?, ?, ?, ?)",
            ("prt_user", "msg_user", "ses_test", 1, '{"type":"text","text":"prompt {not a report}"}'),
        )
        connection.execute(
            "insert into part values (?, ?, ?, ?, ?)",
            ("prt_assistant", "msg_assistant", "ses_test", 2, '{"type":"text","text":"{\\"summary\\":\\"ok\\",\\"pass_check\\":true,\\"score\\":100,\\"findings\\":[]}"}'),
        )
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    assert OpencodeRunner._load_text_parts_from_opencode_db(["ses_test"]) == [
        '{"summary":"ok","pass_check":true,"score":100,"findings":[]}'
    ]


def test_install_project_opencode_config_copies_service_config(monkeypatch, tmp_path: Path) -> None:
    service_dir = tmp_path / "service"
    repo_dir = tmp_path / "repo"
    service_dir.mkdir()
    repo_dir.mkdir()
    (service_dir / "opencode.json").write_text('{"provider":{}}', encoding="utf-8")
    monkeypatch.chdir(service_dir)

    OpencodeRunner._install_project_opencode_config(repo_dir)

    assert (repo_dir / "opencode.json").read_text(encoding="utf-8") == '{"provider":{}}'


def test_install_project_opencode_config_preserves_repo_config(monkeypatch, tmp_path: Path) -> None:
    service_dir = tmp_path / "service"
    repo_dir = tmp_path / "repo"
    service_dir.mkdir()
    repo_dir.mkdir()
    (service_dir / "opencode.json").write_text('{"provider":{"llm-proxy":{}}}', encoding="utf-8")
    (repo_dir / "opencode.json").write_text('{"repo":true}', encoding="utf-8")
    monkeypatch.chdir(service_dir)

    OpencodeRunner._install_project_opencode_config(repo_dir)

    assert (repo_dir / "opencode.json").read_text(encoding="utf-8") == '{"repo":true}'


def test_parse_output_repairs_unquoted_line_range() -> None:
    settings = Settings()
    runner = OpencodeRunner(settings)
    stdout = """
Analysis complete:
{"summary":"ok","pass_check":false,"score":85,"findings":[{"file":"src/main/java/com/foo/Demo.java","line":74-75,"severity":"high","title":"Issue","detail":"Detail","suggestion":"Suggestion"}]}
    """.strip()

    result = runner._parse_output(stdout)

    assert result.score == 85
    assert result.findings[0].line == 74


def test_analyze_retries_with_repair_prompt(monkeypatch, tmp_path) -> None:
    settings = Settings(
        base_dir=tmp_path / "runtime",
        task_dir=tmp_path / "runtime" / "tasks",
        repo_cache_dir=tmp_path / "runtime" / "repos",
        report_dir=tmp_path / "runtime" / "reports",
        log_dir=tmp_path / "logs",
        llm_retry_times=0,
        llm_format_retry_times=1,
    )
    settings.ensure_dirs()
    runner = OpencodeRunner(settings)
    outputs = [
        subprocess.CompletedProcess(
            args=["opencode"],
            returncode=0,
            stdout='分析结束：\n{"summary":"ok","pass_check":false,"score":85,"findings":[{"file":"a.java","line":74-75,"severity":"high","title":"问题","detail":"详情","suggestion":"建议"}]}',
            stderr="",
        ),
        subprocess.CompletedProcess(
            args=["opencode"],
            returncode=0,
            stdout='{"summary":"ok","pass_check":false,"score":85,"findings":[{"file":"a.java","line":74,"severity":"high","title":"问题","detail":"详情","suggestion":"建议"}]}',
            stderr="",
        ),
    ]

    def fake_run_prompt(*, task_id, prompt_file, repo_path, is_cancelled=None):
        return outputs.pop(0)

    monkeypatch.setattr(runner, "_run_prompt", fake_run_prompt)
    monkeypatch.setattr(runner, "_parse_output", lambda text: (_ for _ in ()).throw(RuntimeError("bad json")) if "74-75" in text else OpencodeRunner._parse_output(text))

    result = runner.analyze(
        task_id="task1",
        repo_path=tmp_path,
        branch="feature/test",
        commit_id=None,
        metadata={},
    )

    assert result.score == 85
    assert result.findings[0].line == 74


def test_analyze_retries_once_more_for_suspicious_empty_repair(monkeypatch, tmp_path) -> None:
    settings = Settings(
        base_dir=tmp_path / "runtime",
        task_dir=tmp_path / "runtime" / "tasks",
        repo_cache_dir=tmp_path / "runtime" / "repos",
        report_dir=tmp_path / "runtime" / "reports",
        log_dir=tmp_path / "logs",
        llm_retry_times=0,
        llm_format_retry_times=1,
    )
    settings.ensure_dirs()
    runner = OpencodeRunner(settings)
    outputs = [
        subprocess.CompletedProcess(
            args=["opencode"],
            returncode=0,
            stdout="I'll analyze the git repository changes. Let me start by gathering context from the skill file and the changed files.",
            stderr="",
        ),
        subprocess.CompletedProcess(
            args=["opencode"],
            returncode=0,
            stdout='{"summary":"ok","pass_check":false,"score":85,"findings":[{"file":"a.java","line":74,"severity":"medium","title":"问题","detail":"详情","suggestion":"建议"}]}',
            stderr="",
        ),
    ]

    def fake_run_prompt(*, task_id, prompt_file, repo_path, is_cancelled=None):
        return outputs.pop(0)

    def fake_parse_with_repair_loop(*, task_id, repo_path, raw_output, is_cancelled=None):
        if "I'll analyze" in raw_output or "我会按给定" in raw_output:
            return (
                OpencodeRunner._parse_output('{"summary":"本次变更共审查 3 个非测试文件，未发现阻断发布的问题。","pass_check":true,"score":100,"findings":[]}'),
                1,
            )
        return (
            OpencodeRunner._parse_output(raw_output),
            0,
        )

    monkeypatch.setattr(runner, "_run_prompt", fake_run_prompt)
    monkeypatch.setattr(runner, "_parse_with_repair_loop", fake_parse_with_repair_loop)

    result = runner.analyze(
        task_id="task2",
        repo_path=tmp_path,
        branch="feature/test",
        commit_id=None,
        metadata={},
    )

    assert result.score == 85
    assert len(result.findings) == 1


def test_parse_feedback_output_supports_event_stream() -> None:
    settings = Settings()
    runner = OpencodeRunner(settings)
    stdout = """
{"type":"step_start","timestamp":1}
{"type":"text","timestamp":2,"part":{"type":"text","text":"下面是 JSON：\\n\\n{\\"action\\":\\"resolve_false_positive\\",\\"severity\\":null,\\"reply\\":\\"用户反馈成立，我认为这是误判。\\",\\"pattern_summary\\":\\"调试打印不应在构造性验证中报 fatal\\"}"}}
{"type":"step_finish","timestamp":3}
    """.strip()

    data = runner._parse_feedback_output(stdout)

    assert data["action"] == "resolve_false_positive"
    assert data["pattern_summary"] == "调试打印不应在构造性验证中报 fatal"


def test_parse_fix_apply_output_falls_back_to_plain_text() -> None:
    settings = Settings()
    runner = OpencodeRunner(settings)

    summary = runner._parse_fix_apply_output("Now I'll make the three edits to remove the debug statements:")

    assert summary == "Now I'll make the three edits to remove the debug statements:"


def test_extract_usage_metrics_from_event_stream() -> None:
    settings = Settings()
    runner = OpencodeRunner(settings)
    stdout = """
{"type":"step_start","timestamp":1}
{"type":"response","usage":{"inputTokens":1200,"outputTokens":300,"totalTokens":1500}}
{"type":"step_finish","timestamp":3}
    """.strip()

    usage = runner._extract_usage_metrics(stdout)

    assert usage["prompt_tokens"] == 1200
    assert usage["completion_tokens"] == 300
    assert usage["cache_read_tokens"] == 0
    assert usage["total_tokens"] == 1500


def test_extract_usage_metrics_from_step_finish_tokens_block_reads_cache() -> None:
    settings = Settings()
    runner = OpencodeRunner(settings)
    stdout = """
{"type":"step_finish","timestamp":1774855567560,"sessionID":"ses_x","part":{"id":"prt_x","type":"step-finish","tokens":{"total":24346,"input":21873,"output":26,"reasoning":0,"cache":{"write":0,"read":2447}},"cost":0}}
    """.strip()

    usage = runner._extract_usage_metrics(stdout)

    assert usage["prompt_tokens"] == 21873
    assert usage["completion_tokens"] == 26
    assert usage["thinking_tokens"] == 0
    assert usage["cache_read_tokens"] == 2447
    assert usage["total_tokens"] == 24346


def test_extract_usage_metrics_reads_reasoning_tokens() -> None:
    settings = Settings()
    runner = OpencodeRunner(settings)
    stdout = """
{"type":"step_finish","timestamp":1774855567560,"sessionID":"ses_x","part":{"id":"prt_x","type":"step-finish","tokens":{"total":24346,"input":21873,"output":26,"reasoning":1447,"cache":{"write":0,"read":2447}},"cost":0}}
    """.strip()

    usage = runner._extract_usage_metrics(stdout)

    assert usage["prompt_tokens"] == 21873
    assert usage["completion_tokens"] == 26
    assert usage["thinking_tokens"] == 1447
    assert usage["cache_read_tokens"] == 2447
    assert usage["total_tokens"] == 24346


def test_load_usage_metrics_from_opencode_db_sums_stored_sessions(monkeypatch, tmp_path: Path) -> None:
    db_dir = tmp_path / ".local" / "share" / "opencode"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "opencode.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("create table part (id text primary key, session_id text, time_created integer, data text)")
        connection.execute(
            "insert into part values (?, ?, ?, ?)",
            (
                "prt_1",
                "ses_test",
                1,
                '{"type":"step_finish","part":{"type":"step-finish","tokens":{"total":151600,"input":100000,"output":5000,"reasoning":500}}}',
            ),
        )
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    usage = OpencodeRunner._load_usage_metrics_from_opencode_db(["ses_test"])

    assert usage == {
        "calls": 1,
        "prompt_tokens": 100000,
        "completion_tokens": 5000,
        "thinking_tokens": 500,
        "cache_read_tokens": 0,
        "total_tokens": 151600,
    }


def test_load_usage_metrics_from_opencode_db_includes_child_sessions(monkeypatch, tmp_path: Path) -> None:
    db_dir = tmp_path / ".local" / "share" / "opencode"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "opencode.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("create table session (id text primary key, parent_id text)")
        connection.execute("create table part (id text primary key, session_id text, time_created integer, data text)")
        connection.executemany(
            "insert into session values (?, ?)",
            [
                ("ses_parent", None),
                ("ses_child_1", "ses_parent"),
                ("ses_child_2", "ses_parent"),
                ("ses_unrelated", None),
            ],
        )
        connection.executemany(
            "insert into part values (?, ?, ?, ?)",
            [
                (
                    "prt_parent",
                    "ses_parent",
                    1,
                    '{"type":"step_finish","part":{"type":"step-finish","tokens":{"total":100,"input":50,"output":10,"reasoning":5,"cache":{"read":20}}}}',
                ),
                (
                    "prt_child_1",
                    "ses_child_1",
                    2,
                    '{"type":"step_finish","part":{"type":"step-finish","tokens":{"total":200,"input":60,"output":20,"reasoning":6,"cache":{"read":30}}}}',
                ),
                (
                    "prt_child_2",
                    "ses_child_2",
                    3,
                    '{"type":"step_finish","part":{"type":"step-finish","tokens":{"total":300,"input":70,"output":30,"reasoning":7,"cache":{"read":40}}}}',
                ),
                (
                    "prt_unrelated",
                    "ses_unrelated",
                    4,
                    '{"type":"step_finish","part":{"type":"step-finish","tokens":{"total":999,"input":999,"output":999,"reasoning":999}}}',
                ),
            ],
        )
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    usage = OpencodeRunner._load_usage_metrics_from_opencode_db(["ses_parent"])

    assert usage == {
        "calls": 3,
        "prompt_tokens": 180,
        "completion_tokens": 60,
        "thinking_tokens": 18,
        "cache_read_tokens": 90,
        "total_tokens": 600,
    }

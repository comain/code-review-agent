from cr_agent.review_v2.opencode_stream import OpenCodeStreamParser


def test_stream_parser_extracts_tokens_completion_patches_and_rate_limit() -> None:
    parser = OpenCodeStreamParser()
    events = [
        parser.parse_line(
            '{"type":"text","sessionID":"ses1","part":{"type":"text","text":"final json"}}'
        ),
        parser.parse_line(
            '{"type":"tool_use","part":{"tool":"apply_patch","state":{"status":"completed","output":"Success. Updated files"}}}'
        ),
        parser.parse_line(
            '{"type":"step_finish","cost":0.0042,"part":{"reason":"stop","tokens":{"input":10,"output":5,"reasoning":2,"cache":{"read":3,"write":1},"total":21}}}'
        ),
    ]

    assert parser.extract_text(events) == "final json"
    assert parser.extract_session_id(events) == "ses1"
    assert parser.extract_tokens(events) == {
        "input": 10,
        "output": 5,
        "reasoning": 2,
        "cache": {"read": 3, "write": 1},
        "total": 21,
    }
    assert parser.count_patches(events) == 1
    assert parser.extract_cost(events) == 0.0042
    assert parser.detect_completion(events) == "stop"
    assert parser.detect_rate_limit(
        {"type": "error", "error": {"data": {"statusCode": 429, "message": "too many requests"}}}
    )


def test_stream_parser_ignores_malformed_lines() -> None:
    assert OpenCodeStreamParser().parse_line("{not json") is None

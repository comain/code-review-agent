from cr_agent.review_v2.llm_session import continue_prompt_for_phase, poll_with_continue_recovery
from cr_agent.review_v2.session_analysis import capture_session_token_usage
from cr_agent.review_v2.session_usage import empty_token_bucket, merge_session_token_usage


def test_session_token_usage_merges_shared_buckets() -> None:
    dest = {"total_tokens": empty_token_bucket()}
    merge_session_token_usage(dest, {"total_tokens": {"input": 3, "cache_read": 2, "total": 5}})

    assert dest["total_tokens"]["input"] == 3
    assert dest["total_tokens"]["cache_read"] == 2
    assert dest["total_tokens"]["total"] == 5


def test_capture_session_token_usage_aggregates_multiple_sessions() -> None:
    class Client:
        def analyze_session_tokens(self, session_id):
            return {
                "assistant_messages": 1,
                "total_tokens": {"input": 1, "output": 2, "reasoning": 0, "cache_read": 3, "cache_write": 0, "total": 6},
                "main_model_tokens": {},
                "small_model_tokens": {},
                "other_model_tokens": {},
                "by_model": {session_id: {"total": 6}},
            }

    usage = capture_session_token_usage(state={}, client=Client(), session_ids=["s1", "s2"])

    assert usage["assistant_messages"] == 2
    assert usage["total_tokens"]["input"] == 2
    assert usage["total_tokens"]["cache_read"] == 6
    assert usage["total_tokens"]["total"] == 12


def test_poll_with_continue_recovery_sends_one_guarded_continue() -> None:
    class Client:
        def __init__(self):
            self.sent = []
            self.calls = 0

        def poll_completion(self, session_id, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return {"type": "stalled_no_progress", "reason": "idle"}
            return {"type": "completed"}

        def send_message(self, session_id, message, model_id=None):
            self.sent.append((session_id, message, model_id))

    client = Client()

    event = poll_with_continue_recovery(
        client=client,
        session_id="ses1",
        timeout=300,
        phase="review",
        model_id="llm-proxy/gpt-5.5",
    )

    assert event["type"] == "completed"
    assert len(client.sent) == 1
    assert client.sent[0][1] == continue_prompt_for_phase("review")

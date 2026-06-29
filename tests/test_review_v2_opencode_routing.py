import json

from cr_agent.config import Settings
from cr_agent.review_v2.opencode_config import (
    _select_candidate,
    build_opencode_config,
    candidate_opencode_models,
    per_turn_opencode_config_dir,
    reset_model_availability_cache,
)
from cr_agent.review_v2.opencode_routing import (
    mark_model_unhealthy,
    model_health_for_candidates,
    opencode_model_id,
    parse_provider_base_urls,
    parse_provider_chain,
    parse_provider_tokens,
    provider_token_statuses,
    reset_model_health,
)


def test_llm_proxy_opencode_config_uses_required_base_url_without_api_key() -> None:
    settings = Settings()

    config = build_opencode_config(settings)
    raw = json.dumps(config)

    llm_proxy = config["provider"]["llm-proxy"]
    assert llm_proxy["options"]["baseURL"] == "http://openai-compatible.example.com/v1"
    assert "apiKey" not in llm_proxy["options"]
    assert "llm-proxy/gpt-5.5" in config["model"]
    assert "apiKey" not in raw
    assert config["permission"]["edit"] == "deny"
    assert config["permission"]["question"] == "deny"
    assert config["permission"]["doom_loop"] == "allow"


def test_per_turn_opencode_config_allows_project_external_directory(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = Settings(base_dir=tmp_path / "runtime")

    with per_turn_opencode_config_dir(repo, settings, label="reviewer") as config_dir:
        config = json.loads((config_dir / "opencode.json").read_text(encoding="utf-8"))

    permission = config["permission"]
    assert permission["*"] == "allow"
    assert permission["edit"] == "deny"
    assert permission["external_directory"] == {f"{repo.resolve()}/**": "allow"}


def test_llm_proxy_base_url_can_be_overridden_without_trailing_slash() -> None:
    settings = Settings(
        review_v2_llm_proxy_base_url="http://example.test/v1/",
        review_v2_opencode_provider_base_urls="",
    )

    config = build_opencode_config(settings)

    assert config["provider"]["llm-proxy"]["options"]["baseURL"] == "http://example.test/v1"


def test_opencode_config_registers_provider_chain_models_and_tokens() -> None:
    settings = Settings(
        review_v2_opencode_provider_chain="llm-proxy:claude-fable-5,gpt-5.5;openai:openai/gpt-5.5",
        review_v2_opencode_provider_tokens="llm-proxy.token=tp-secret;openai.token=oa-secret",
        review_v2_opencode_provider_base_urls=(
            "llm-proxy.base_url=http://llm-proxy.example/v1;"
            "openai.base_url=https://openai.example/v1"
        ),
        review_v2_opencode_model_api_timeout_seconds=0,
    )

    config = build_opencode_config(settings)

    assert config["model"] == "llm-proxy/claude-fable-5"
    assert set(config["provider"]["llm-proxy"]["models"]) == {"claude-fable-5", "gpt-5.5"}
    assert set(config["provider"]["openai"]["models"]) == {"gpt-5.5"}
    assert config["provider"]["llm-proxy"]["options"]["baseURL"] == "http://llm-proxy.example/v1"
    assert config["provider"]["llm-proxy"]["options"]["apiKey"] == "tp-secret"
    assert config["provider"]["openai"]["options"]["apiKey"] == "oa-secret"


def test_opencode_model_selection_skips_unavailable_first_model() -> None:
    settings = Settings(
        review_v2_opencode_provider_chain="llm-proxy:claude-fable-5,gpt-5.5;openai:openai/gpt-5.5",
        review_v2_opencode_provider_tokens="llm-proxy.token=tp-secret;openai.token=oa-secret",
    )

    selected = _select_candidate(
        settings,
        fetch_available_models=lambda provider, _settings: {
            "llm-proxy": {"gpt-5.5"},
            "openai": {"openai/gpt-5.5"},
        }[provider],
    )

    assert opencode_model_id(selected) == "llm-proxy/gpt-5.5"


def test_provider_model_api_availability_is_cached(monkeypatch) -> None:
    reset_model_availability_cache()
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"data":[{"id":"gpt-5.5"}]}'

    def fake_urlopen(request, timeout):
        calls.append((request.full_url, timeout))
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    settings = Settings(
        review_v2_opencode_provider_chain="llm-proxy:claude-fable-5,gpt-5.5",
        review_v2_opencode_provider_tokens="llm-proxy.token=tp-secret",
        review_v2_opencode_provider_base_urls="llm-proxy.base_url=http://llm-proxy.example/v1",
        review_v2_opencode_model_api_timeout_seconds=5,
        review_v2_opencode_model_api_cache_seconds=300,
    )

    assert opencode_model_id(_select_candidate(settings)) == "llm-proxy/gpt-5.5"
    assert candidate_opencode_models(settings) == ["llm-proxy/gpt-5.5"]
    assert len(calls) == 1
    reset_model_availability_cache()


def test_provider_chain_and_redacted_token_statuses() -> None:
    candidates = parse_provider_chain("llm-proxy:gpt-5.5,gpt-5.4;openai:gpt-5")
    tokens = parse_provider_tokens("llm-proxy.token=secret;openai.token=")
    base_urls = parse_provider_base_urls("llm-proxy.base_url=http://tp/v1;openai.baseURL=https://oa/v1/")

    assert [opencode_model_id(candidate) for candidate in candidates] == [
        "llm-proxy/gpt-5.5",
        "llm-proxy/gpt-5.4",
        "openai/gpt-5",
    ]
    assert provider_token_statuses(candidates, tokens) == {
        "llm-proxy": "configured",
        "openai": "missing",
    }
    assert base_urls == {
        "llm-proxy": "http://tp/v1",
        "openai": "https://oa/v1",
    }


def test_model_health_tracks_unhealthy_candidates() -> None:
    reset_model_health()
    candidates = parse_provider_chain("llm-proxy:gpt-5.5;openai:gpt-5")

    mark_model_unhealthy("llm-proxy/gpt-5.5", reason="model_not_found", retry_after_seconds=60)
    health = model_health_for_candidates(candidates)

    assert health["llm-proxy/gpt-5.5"]["reason"] == "model_not_found"
    assert "openai/gpt-5" not in health
    reset_model_health()


def test_model_selection_skips_unhealthy_first_candidate() -> None:
    reset_model_health()
    try:
        settings = Settings(review_v2_opencode_provider_chain="llm-proxy:gpt-5.5;openai:gpt-5")

        mark_model_unhealthy("llm-proxy/gpt-5.5", reason="provider_error", retry_after_seconds=60)

        selected = _select_candidate(settings)
        assert selected
        assert opencode_model_id(selected) == "openai/gpt-5"
        assert candidate_opencode_models(settings) == ["openai/gpt-5"]
    finally:
        reset_model_health()

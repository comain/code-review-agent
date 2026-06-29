"""OpenCode config generation adapted from comain/unit-test-agent `reference/opencode/config.py`."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import time
from typing import Callable, Iterator, Optional, Set
import urllib.request
import uuid

from cr_agent.config import Settings
from cr_agent.review_v2.opencode_routing import (
    ProviderCandidate,
    is_model_healthy,
    opencode_model_id,
    parse_provider_base_urls,
    parse_provider_chain,
    parse_provider_tokens,
    provider_local_model_id,
)


_model_api_cache: dict[str, tuple[float, Optional[Set[str]]]] = {}


def _provider_candidates(settings: Settings) -> list[ProviderCandidate]:
    chain = (settings.review_v2_opencode_provider_chain or "").strip()
    candidates = parse_provider_chain(chain)
    if candidates:
        return candidates
    return [ProviderCandidate(provider="llm-proxy", model="gpt-5.5", index=0)]


def selected_opencode_model(settings: Settings) -> str:
    return opencode_model_id(_select_candidate(settings))


def candidate_opencode_models(settings: Settings, *, preferred_model: str | None = None) -> list[str]:
    models = [opencode_model_id(candidate) for candidate in _select_candidates(settings)]
    if preferred_model:
        ordered = [preferred_model]
        ordered.extend(model for model in models if model != preferred_model)
        return ordered
    return models


def _provider_api_key(provider_id: str, settings: Settings) -> str:
    return parse_provider_tokens(settings.review_v2_opencode_provider_tokens).get(provider_id, "")


def _provider_base_url(provider_id: str, settings: Settings) -> str:
    configured = parse_provider_base_urls(settings.review_v2_opencode_provider_base_urls)
    if configured.get(provider_id):
        return configured[provider_id]
    if provider_id == "llm-proxy":
        return settings.review_v2_llm_proxy_base_url.rstrip("/")
    return ""


def _model_api_url(provider_id: str, settings: Settings) -> str:
    base_url = _provider_base_url(provider_id, settings).rstrip("/")
    if not base_url:
        return ""
    if base_url.endswith("/v1"):
        return f"{base_url}/models"
    return f"{base_url}/v1/models"


def _parse_model_list_response(payload: object) -> Set[str]:
    raw_items: object
    if isinstance(payload, dict):
        raw_items = payload.get("data")
        if raw_items is None:
            raw_items = payload.get("models")
    else:
        raw_items = payload
    if not isinstance(raw_items, list):
        return set()
    model_ids: Set[str] = set()
    for item in raw_items:
        if isinstance(item, str) and item.strip():
            model_ids.add(item.strip())
        elif isinstance(item, dict):
            model_id = item.get("id")
            if isinstance(model_id, str) and model_id.strip():
                model_ids.add(model_id.strip())
    return model_ids


def _provider_available_models(provider_id: str, settings: Settings) -> Optional[Set[str]]:
    token = _provider_api_key(provider_id, settings)
    if not token:
        return None
    url = _model_api_url(provider_id, settings)
    if not url:
        return None
    ttl = max(0, int(settings.review_v2_opencode_model_api_cache_seconds or 0))
    now = time.time()
    cached = _model_api_cache.get(url)
    if cached and ttl > 0 and now - cached[0] < ttl:
        return cached[1]
    timeout = float(settings.review_v2_opencode_model_api_timeout_seconds or 0)
    if timeout <= 0:
        return None
    request = urllib.request.Request(url)
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            available = _parse_model_list_response(json.loads(response.read().decode("utf-8", "replace")))
    except Exception:
        _model_api_cache[url] = (now, None)
        return None
    _model_api_cache[url] = (now, available)
    return available


def reset_model_availability_cache() -> None:
    _model_api_cache.clear()


def _candidate_model_ids(candidate: ProviderCandidate) -> Set[str]:
    ids = {candidate.model, opencode_model_id(candidate)}
    local_id = provider_local_model_id(candidate.provider, candidate.model)
    if local_id:
        ids.add(local_id)
    return ids


def _select_candidate(
    settings: Settings,
    *,
    fetch_available_models: Callable[[str, Settings], Optional[Set[str]]] = _provider_available_models,
) -> ProviderCandidate:
    selected = _select_candidates(settings, fetch_available_models=fetch_available_models)
    if selected:
        return selected[0]
    return _provider_candidates(settings)[0]


def _select_candidates(
    settings: Settings,
    *,
    fetch_available_models: Callable[[str, Settings], Optional[Set[str]]] = _provider_available_models,
) -> list[ProviderCandidate]:
    candidates = _provider_candidates(settings)
    by_provider: dict[str, Optional[Set[str]]] = {}
    selected: list[ProviderCandidate] = []
    for candidate in candidates:
        model_id = opencode_model_id(candidate)
        if not is_model_healthy(model_id):
            continue
        if candidate.provider not in by_provider:
            by_provider[candidate.provider] = fetch_available_models(candidate.provider, settings)
        available = by_provider[candidate.provider]
        if available is None or _candidate_model_ids(candidate) & available:
            selected.append(candidate)
    return selected


def _model_limit(provider_id: str) -> dict:
    if provider_id in {"llm-proxy", "openai", "deepseek"}:
        return {
            "context": 272000,
            "output": 128000,
        }
    return {
        "context": 262144,
        "output": 32768,
    }


def _register_provider_models(config: dict, settings: Settings, provider_id: str, models: list[str]) -> None:
    provider = config.setdefault("provider", {}).setdefault(provider_id, {"models": {}})
    provider_base_url = _provider_base_url(provider_id, settings)
    if provider_base_url:
        provider.setdefault("npm", "@ai-sdk/openai-compatible")
        provider.setdefault(
            "name",
            {
                "llm-proxy": "LLM Proxy",
                "openai": "OpenAI-compatible",
                "deepseek": "DeepSeek OpenAI-compatible",
            }.get(provider_id, provider_id),
        )
        provider.setdefault("options", {})
        provider["options"].setdefault("baseURL", provider_base_url)
        api_key = _provider_api_key(provider_id, settings)
        if api_key:
            provider["options"].setdefault("apiKey", api_key)
    for full_model in models:
        model_id = provider_local_model_id(provider_id, full_model)
        if not model_id:
            continue
        provider["models"].setdefault(
            model_id,
            {
                "name": model_id,
                "limit": _model_limit(provider_id),
            },
        )


def build_opencode_config(settings: Settings, *, model_id: str | None = None, project_root: Path | None = None) -> dict:
    candidates = _provider_candidates(settings)
    executable_model = model_id or opencode_model_id(_select_candidate(settings))
    provider_models: dict[str, list[str]] = {}
    for candidate in candidates:
        provider_models.setdefault(candidate.provider, []).append(candidate.model)
    if "/" in executable_model:
        provider_id, local_model = executable_model.split("/", 1)
        provider_models.setdefault(provider_id, [])
        if local_model not in provider_models[provider_id]:
            provider_models[provider_id].append(local_model)

    config = {
        "model": executable_model,
        "small_model": executable_model,
        "provider": {},
        "permission": _review_permission(project_root),
    }
    for provider_id in sorted(provider_models):
        _register_provider_models(config, settings, provider_id, provider_models[provider_id])
    return config


def _review_permission(project_root: Path | None = None) -> dict:
    permission = {
        "*": "allow",
        "edit": "deny",
        "question": "deny",
        "doom_loop": "allow",
    }
    if project_root is not None:
        root = str(project_root.resolve())
        permission["external_directory"] = {
            f"{root}/**": "allow",
        }
    return permission


@contextmanager
def per_turn_opencode_config_dir(
    repo_path: Path,
    settings: Settings,
    *,
    label: str | None = None,
    model_id: str | None = None,
) -> Iterator[Path]:
    safe_label = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in (label or "turn"))[:48]
    repo_path = repo_path.resolve()
    config_dir = (
        repo_path
        / ".cr_agent"
        / "opencode"
        / "configs"
        / f"{int(time.time() * 1000)}-{safe_label}-{uuid.uuid4().hex[:8]}"
    )
    config_dir.mkdir(parents=True, exist_ok=False)
    _link_repo_project_files(repo_path, config_dir)
    (config_dir / "opencode.json").write_text(
        json.dumps(
            build_opencode_config(settings, model_id=model_id, project_root=repo_path),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    try:
        yield config_dir
    finally:
        shutil.rmtree(config_dir, ignore_errors=True)


def _link_repo_project_files(repo_path: Path, project_dir: Path) -> None:
    for child in repo_path.iterdir():
        if child.name in {"opencode.json", ".cr_agent"}:
            continue
        target = project_dir / child.name
        if child.name == ".git" and child.is_file():
            target.write_text(_absolute_gitdir_file(repo_path, child), encoding="utf-8")
            continue
        os.symlink(child, target, target_is_directory=child.is_dir())


def _absolute_gitdir_file(repo_path: Path, git_file: Path) -> str:
    text = git_file.read_text(encoding="utf-8")
    prefix = "gitdir:"
    if not text.startswith(prefix):
        return text
    gitdir = text[len(prefix) :].strip()
    gitdir_path = Path(gitdir)
    if not gitdir_path.is_absolute():
        gitdir_path = (repo_path / gitdir_path).resolve()
    return f"gitdir: {gitdir_path}\n"

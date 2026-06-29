"""Provider-chain routing adapted from comain/unit-test-agent `reference/opencode/tiered_router.py`."""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Dict, Iterable, List, Optional


_DEFAULT_COOLDOWN_SECONDS = 120
_MODEL_UNAVAILABLE_COOLDOWN_SECONDS = 15 * 60
_NO_OUTPUT_COOLDOWN_SECONDS = 10 * 60


@dataclass(frozen=True)
class ProviderCandidate:
    provider: str
    model: str
    index: int


def provider_local_model_id(provider_id: str, model_id: str) -> str:
    if model_id.startswith(f"{provider_id}/"):
        return model_id.split("/", 1)[1]
    return model_id


def opencode_model_id(candidate: ProviderCandidate) -> str:
    if not candidate.provider or "/" in candidate.model:
        return candidate.model
    return f"{candidate.provider}/{candidate.model}"


def parse_provider_chain(raw: str) -> List[ProviderCandidate]:
    candidates: List[ProviderCandidate] = []
    for provider_group in (raw or "").split(";"):
        group = provider_group.strip()
        if not group or ":" not in group:
            continue
        provider, raw_models = group.split(":", 1)
        provider = provider.strip()
        if not provider:
            continue
        for raw_model in raw_models.split(","):
            model = raw_model.strip()
            if model:
                candidates.append(ProviderCandidate(provider=provider, model=model, index=len(candidates)))
    return candidates


def parse_provider_tokens(raw: str) -> Dict[str, str]:
    tokens: Dict[str, str] = {}
    for raw_entry in (raw or "").split(";"):
        entry = raw_entry.strip()
        if not entry or "=" not in entry:
            continue
        raw_key, raw_value = entry.split("=", 1)
        key = raw_key.strip()
        value = raw_value.strip()
        if not key.endswith(".token") or not value:
            continue
        provider = key[: -len(".token")].strip()
        if provider:
            tokens[provider] = value
    return tokens


def parse_provider_base_urls(raw: str) -> Dict[str, str]:
    urls: Dict[str, str] = {}
    accepted_suffixes = (".base_url", ".baseURL", ".base-url", ".baseurl")
    for raw_entry in (raw or "").split(";"):
        entry = raw_entry.strip()
        if not entry or "=" not in entry:
            continue
        raw_key, raw_value = entry.split("=", 1)
        key = raw_key.strip()
        value = raw_value.strip().rstrip("/")
        if not value:
            continue
        for suffix in accepted_suffixes:
            if key.endswith(suffix):
                provider = key[: -len(suffix)].strip()
                if provider:
                    urls[provider] = value
                break
    return urls


def provider_token_statuses(
    chain: Iterable[ProviderCandidate],
    tokens: Dict[str, str],
) -> Dict[str, str]:
    statuses: Dict[str, str] = {}
    for candidate in chain:
        if candidate.provider in statuses:
            continue
        statuses[candidate.provider] = "configured" if tokens.get(candidate.provider) else "missing"
    return statuses


class ModelHealthTracker:
    def __init__(self) -> None:
        self._unhealthy_until: Dict[str, float] = {}
        self._reasons: Dict[str, str] = {}

    def mark_unhealthy(
        self,
        model_id: str,
        *,
        reason: str = "model_unavailable",
        retry_after_seconds: Optional[int] = None,
    ) -> None:
        cooldown = self._cooldown_seconds(reason, retry_after_seconds)
        self._unhealthy_until[model_id] = time.time() + cooldown
        self._reasons[model_id] = reason or "model_unavailable"

    def is_healthy(self, model_id: str) -> bool:
        until = self._unhealthy_until.get(model_id)
        if until is None:
            return True
        if time.time() >= until:
            self._unhealthy_until.pop(model_id, None)
            self._reasons.pop(model_id, None)
            return True
        return False

    def status(self, model_id: str) -> Optional[Dict[str, float]]:
        if self.is_healthy(model_id):
            return None
        return {
            "reason": self._reasons.get(model_id) or "model_unavailable",
            "unhealthy_until": self._unhealthy_until[model_id],
        }

    def reset(self) -> None:
        self._unhealthy_until.clear()
        self._reasons.clear()

    @staticmethod
    def _cooldown_seconds(reason: str, retry_after_seconds: Optional[int]) -> int:
        if retry_after_seconds and retry_after_seconds > 0:
            return int(retry_after_seconds)
        if reason == "no_output":
            return _NO_OUTPUT_COOLDOWN_SECONDS
        if reason and reason != "rate_limit":
            return _MODEL_UNAVAILABLE_COOLDOWN_SECONDS
        return _DEFAULT_COOLDOWN_SECONDS


_tracker = ModelHealthTracker()


def mark_model_unhealthy(
    model_id: str,
    *,
    reason: str = "model_unavailable",
    retry_after_seconds: Optional[int] = None,
) -> None:
    _tracker.mark_unhealthy(model_id, reason=reason, retry_after_seconds=retry_after_seconds)


def model_health_for_candidates(candidates: Iterable[ProviderCandidate]) -> Dict[str, Dict[str, float]]:
    health: Dict[str, Dict[str, float]] = {}
    for candidate in candidates:
        model_id = opencode_model_id(candidate)
        status = _tracker.status(model_id)
        if status:
            health[model_id] = status
    return health


def is_model_healthy(model_id: str) -> bool:
    return _tracker.is_healthy(model_id)


def reset_model_health() -> None:
    _tracker.reset()

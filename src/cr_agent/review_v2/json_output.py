"""Deterministic JSON extraction for reviewer outputs."""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, Optional


def extract_json_object(text: str, *, required_keys: Iterable[str] = ()) -> Dict[str, Any]:
    decoder = json.JSONDecoder()
    content = text or "{}"
    required = set(required_keys)
    fallback: Optional[Dict[str, Any]] = None
    match: Optional[Dict[str, Any]] = None
    for index, char in enumerate(content):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(content[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            fallback = value
            if required.issubset(value):
                match = value
    if required and match is not None:
        return match
    if not required and fallback is not None:
        return fallback
    if fallback is None:
        raise json.JSONDecodeError("no JSON object found", content, 0)
    raise json.JSONDecodeError("no JSON object matching required keys found", content, 0)

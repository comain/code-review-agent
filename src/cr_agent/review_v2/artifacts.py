"""Private audit artifact helpers for CR v2."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict


class ArtifactStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def write_text(self, name: str, content: str, *, max_bytes: int = 0) -> Dict[str, Any]:
        raw = content.encode("utf-8")
        truncated = False
        if max_bytes > 0 and len(raw) > max_bytes:
            raw = raw[:max_bytes]
            truncated = True
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return {
            "path": str(path),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
            "truncated": truncated,
        }

    def write_json(self, name: str, value: Any) -> Dict[str, Any]:
        return self.write_text(
            name,
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True),
        )

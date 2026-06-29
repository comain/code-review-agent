from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


class UsageStore:
    def __init__(self, usage_dir: Path) -> None:
        self.usage_dir = usage_dir
        self.usage_dir.mkdir(parents=True, exist_ok=True)

    def append(
        self,
        *,
        task_id: str,
        category: str,
        prompt_tokens: int,
        completion_tokens: int,
        thinking_tokens: int,
        total_tokens: int,
        returncode: int,
        created_at: Optional[datetime] = None,
    ) -> Path:
        current = created_at or datetime.now()
        day = current.strftime("%Y-%m-%d")
        path = self.usage_dir / f"{day}.usage.jsonl"
        entry = {
            "task_id": task_id,
            "category": category,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "thinking_tokens": thinking_tokens,
            "total_tokens": total_tokens,
            "returncode": returncode,
            "created_at": current.isoformat(),
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return path

    def list_all(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for path in sorted(self.usage_dir.glob("*.usage.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                item = json.loads(line)
                item["created_at"] = datetime.fromisoformat(item["created_at"])
                rows.append(item)
        return rows

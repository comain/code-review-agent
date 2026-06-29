from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from cr_agent.models import TaskRecord


class TaskStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._cache: Dict[str, TaskRecord] = {}

    def _path(self, task_id: str) -> Path:
        return self.root / f"{task_id}.json"

    def save(self, record: TaskRecord) -> TaskRecord:
        with self._lock:
            record.touch()
            self._cache[record.task_id] = record
            path = self._path(record.task_id)
            tmp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
            tmp_path.write_text(
                record.model_dump_json(indent=2),
                encoding="utf-8",
            )
            tmp_path.replace(path)
            return record

    def get(self, task_id: str) -> Optional[TaskRecord]:
        with self._lock:
            if task_id in self._cache:
                return self._cache[task_id]
            path = self._path(task_id)
            if not path.exists():
                return None
            record = self._load_record(path)
            self._cache[task_id] = record
            return record

    def list_all(self) -> List[TaskRecord]:
        with self._lock:
            records: List[TaskRecord] = []
            for path in sorted(self.root.glob("*.json")):
                task_id = path.stem
                record = self.get(task_id)
                if record is not None:
                    records.append(record)
            return records

    def delete(self, task_id: str) -> None:
        with self._lock:
            self._cache.pop(task_id, None)
            path = self._path(task_id)
            if path.exists():
                path.unlink()

    @staticmethod
    def _load_record(path: Path) -> TaskRecord:
        last_error: Optional[Exception] = None
        for _ in range(3):
            try:
                return TaskRecord.model_validate(json.loads(path.read_text(encoding="utf-8")))
            except json.JSONDecodeError as exc:
                last_error = exc
                time.sleep(0.02)
        raise last_error if last_error is not None else RuntimeError(f"failed to load task file: {path}")

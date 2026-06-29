from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List

import httpx

from cr_agent.config import Settings
from cr_agent.models import CallbackPayload, TaskRecord

logger = logging.getLogger(__name__)


class CallbackClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def send(self, record: TaskRecord, payload: CallbackPayload) -> List[Dict[str, Any]]:
        callback_url, callback_body = self._build_request(record, payload)
        history: List[Dict[str, Any]] = []
        headers = {"Content-Type": "application/json"}
        token = record.request.callback_token
        if token:
            headers["X-Task-Token"] = token

        logger.info(
            "task=%s callback request body=%s",
            record.task_id,
            json.dumps(callback_body, ensure_ascii=False, sort_keys=True),
        )

        for attempt in range(1, self.settings.callback_retry_times + 2):
            started = time.time()
            try:
                logger.info(
                    "task=%s callback attempt=%s/%s target=%s start",
                    record.task_id,
                    attempt,
                    self.settings.callback_retry_times + 1,
                    callback_url,
                )
                with httpx.Client(timeout=self.settings.callback_timeout_seconds) as client:
                    response = client.post(callback_url, headers=headers, json=callback_body)
                entry = {
                    "attempt": attempt,
                    "status_code": response.status_code,
                    "elapsed_ms": int((time.time() - started) * 1000),
                    "body": response.text[:1000],
                }
                history.append(entry)
                logger.info(
                    "task=%s callback attempt=%s/%s finished status_code=%s elapsed_ms=%s",
                    record.task_id,
                    attempt,
                    self.settings.callback_retry_times + 1,
                    response.status_code,
                    entry["elapsed_ms"],
                )
                if 200 <= response.status_code < 300:
                    return history
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "task=%s callback attempt=%s/%s failed error=%s",
                    record.task_id,
                    attempt,
                    self.settings.callback_retry_times + 1,
                    exc,
                )
                history.append(
                    {
                        "attempt": attempt,
                        "elapsed_ms": int((time.time() - started) * 1000),
                        "error": str(exc),
                    }
                )
            time.sleep(min(attempt, 3))

        raise RuntimeError(f"callback failed after retries: {history}")

    def _build_request(self, record: TaskRecord, payload: CallbackPayload) -> tuple[str, Dict[str, Any]]:
        if record.request.callback_url is not None:
            return str(record.request.callback_url), payload.model_dump(mode="json")

        if record.request.ci_task_id and record.request.ci_record_id:
            state = 0 if payload.passed else -1024
            body = {
                "state": state,
                "attribute": {
                    "taskId": record.request.ci_task_id,
                    "recordId": record.request.ci_record_id,
                    "taskTemplateId": record.request.ci_task_template_id or "",
                    "parentId": record.request.ci_parent_id or "",
                    "url": payload.report_url,
                    "reportUrl": payload.report_url,
                    "operator": record.request.operator or "",
                },
                "data": {
                    "score": str(payload.score),
                    "passed": str(payload.passed).lower(),
                    "summary": payload.summary,
                },
            }
            return self.settings.ci_ack_url, body

        raise RuntimeError("no callback target configured")

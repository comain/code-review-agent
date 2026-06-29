from __future__ import annotations

import html
import hmac
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from cr_agent.dependencies import get_task_service
from cr_agent.core.service import TaskService
from cr_agent.models import TaskRecord, TriggerRequest, TriggerResponse
from cr_agent.review_v2.feedback import FeedbackProcessor
from cr_agent.review_v2.feedback_patterns import sync_feedback_patterns_from_db
from cr_agent.review_v2.views import ReviewV2Views

router = APIRouter()
_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
_SYNC_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "sync_false_positive_patterns.py"
_sync_lock = threading.Lock()
_sync_state: Dict[str, Any] = {
    "running": False,
    "last_started_at": None,
    "last_finished_at": None,
    "last_exit_code": None,
    "last_message": "尚未触发",
}


def _ci_result(status: int, message: str, data: Dict[str, Any]) -> JSONResponse:
    return JSONResponse(
        status_code=200,
        content={
            "status": status,
            "errcode": status,
            "msg": message,
            "message": message,
            "data": data,
        },
    )


def _task_status_url(request: Request, task_id: str) -> str:
    return f"{str(request.base_url).rstrip('/')}/task-status/{task_id}"


def _report_or_task_url(request: Request, record: TaskRecord) -> str:
    return record.report_url or _task_status_url(request, record.task_id)


def _require_trigger_auth(request: Request, service: TaskService = Depends(get_task_service)) -> None:
    expected = service.settings.trigger_token.strip()
    if not expected:
        raise HTTPException(status_code=503, detail="trigger token is not configured")
    provided = _trigger_token_from_request(request)
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="invalid trigger token")


def _trigger_token_from_request(request: Request) -> str:
    authorization = request.headers.get("authorization", "")
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    return (
        request.headers.get("x-cr-agent-token")
        or request.headers.get("x-webhook-token")
        or ""
    ).strip()


CR_V2_LOGIC_DOC_URL = (
    "https://github.com/comain/code-review-agent/blob/osc/README.md"
    "#cr-v2-overview"
)


def _execute_false_positive_sync_script() -> None:
    with _sync_lock:
        _sync_state["running"] = True
        _sync_state["last_started_at"] = datetime.now().isoformat()
        _sync_state["last_message"] = "同步任务执行中"
    env = os.environ.copy()
    completed = subprocess.run(
        [sys.executable, str(_SYNC_SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(_SYNC_SCRIPT.parent.parent),
    )
    output = (completed.stdout or completed.stderr or "").strip()
    with _sync_lock:
        _sync_state["running"] = False
        _sync_state["last_finished_at"] = datetime.now().isoformat()
        _sync_state["last_exit_code"] = completed.returncode
        _sync_state["last_message"] = output[:2000] if output else ("同步完成" if completed.returncode == 0 else "同步失败")


def _run_v2_false_positive_sync(service: TaskService) -> Dict[str, Any]:
    with _sync_lock:
        if _sync_state["running"]:
            return {
                "accepted": False,
                "message": "同步任务已在执行中",
                **_sync_state,
            }
        _sync_state["running"] = True
        _sync_state["last_started_at"] = datetime.now().isoformat()
        _sync_state["last_message"] = "CR v2 SQLite feedback 范式同步执行中"
    try:
        result = sync_feedback_patterns_from_db(service.review_v2_db, service.settings)
        payload = result.to_dict()
        with _sync_lock:
            _sync_state["running"] = False
            _sync_state["last_finished_at"] = datetime.now().isoformat()
            _sync_state["last_exit_code"] = 0
            _sync_state["last_message"] = (
                f"CR v2 SQLite feedback 范式同步完成: total={result.total_count}, changed={result.changed}"
            )
        return {
            "accepted": True,
            "message": "已完成 CR v2 SQLite feedback 范式同步",
            "result": payload,
            **_sync_state,
        }
    except Exception as exc:  # noqa: BLE001
        with _sync_lock:
            _sync_state["running"] = False
            _sync_state["last_finished_at"] = datetime.now().isoformat()
            _sync_state["last_exit_code"] = 1
            _sync_state["last_message"] = str(exc)[:2000]
        return {
            "accepted": False,
            "message": "CR v2 SQLite feedback 范式同步失败",
            "error": str(exc),
            **_sync_state,
        }


def _trigger_false_positive_sync(service: Optional[TaskService] = None) -> Dict[str, Any]:
    if service is not None and service.settings.review_v2_enabled:
        return _run_v2_false_positive_sync(service)
    with _sync_lock:
        if _sync_state["running"]:
            return {
                "accepted": False,
                "message": "同步任务已在执行中",
                **_sync_state,
            }
        thread = threading.Thread(
            target=_execute_false_positive_sync_script,
            name="cr-agent-false-positive-sync",
            daemon=True,
        )
        thread.start()
        return {
            "accepted": True,
            "message": "已触发范式同步任务",
            **_sync_state,
        }


def _to_shanghai_label(value: Optional[datetime], include_seconds: bool = True) -> str:
    if value is None:
        return "暂无"
    local = value.astimezone(ZoneInfo("Asia/Shanghai"))
    return local.strftime("%Y-%m-%d %H:%M:%S" if include_seconds else "%Y-%m-%d %H:%M")


def _iso_to_shanghai_label(value: Optional[str], include_seconds: bool = True) -> str:
    if not value:
        return "暂无"
    return _to_shanghai_label(datetime.fromisoformat(value.replace("Z", "+00:00")), include_seconds=include_seconds)


def _escape(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _json_for_script(value: Any) -> str:
    return (
        json.dumps(value, ensure_ascii=False)
        .replace("</", "<\\/")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _finding_location(item: Dict[str, Any]) -> str:
    path = item.get("file") or item.get("file_path") or "-"
    line = item.get("line")
    return f"{path}:{line}" if line else str(path)


_SEVERITY_ORDER = ("fatal", "high", "medium", "low", "info")
_BLOCKING_SEVERITIES = {"fatal", "high", "medium"}


def _finding_severity(item: Dict[str, Any]) -> str:
    severity = str(item.get("effective_severity") or item.get("severity") or item.get("original_severity") or "info").lower()
    return severity if severity in _SEVERITY_ORDER else "info"


def _severity_rank(severity: str) -> int:
    try:
        return _SEVERITY_ORDER.index(severity)
    except ValueError:
        return len(_SEVERITY_ORDER)


def _severity_group_title(severity: str, count: int) -> str:
    layer = "Blocking" if severity in _BLOCKING_SEVERITIES else "Non-blocking"
    return f"{layer} · {severity} ({count})"


def _render_findings_html(findings: List[Dict[str, Any]], *, include_feedback_actions: bool = False) -> str:
    if not findings:
        return '<p class="muted">未发现问题。</p>'
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for item in sorted(findings, key=lambda finding: (_severity_rank(_finding_severity(finding)), _finding_location(finding))):
        grouped.setdefault(_finding_severity(item), []).append(item)
    sections: List[str] = []
    for severity in _SEVERITY_ORDER:
        items = grouped.get(severity) or []
        if not items:
            continue
        layer = "blocking" if severity in _BLOCKING_SEVERITIES else "non-blocking"
        findings_html = "\n".join(
            _render_finding_card(item, index, include_feedback_actions=include_feedback_actions)
            for index, item in enumerate(items, start=1)
        )
        sections.append(
            f"""
            <section class="severity-group severity-{severity} {layer}" aria-label="{_escape(_severity_group_title(severity, len(items)))}">
              <h3>{_escape(_severity_group_title(severity, len(items)))}</h3>
              {findings_html}
            </section>
            """
        )
    return "\n".join(sections)


def _render_finding_card(item: Dict[str, Any], index: int, *, include_feedback_actions: bool) -> str:
    severity = _finding_severity(item)
    layer_label = "Blocking" if severity in _BLOCKING_SEVERITIES else "Non-blocking"
    reviewer_badge = (
        f'<span class="badge">reviewer: {_escape(item.get("source_reviewer"))}</span>'
        if item.get("source_reviewer")
        else ""
    )
    return (
        f"""
        <article class="finding severity-{severity}">
          <h3>{index}. {_escape(item.get("title") or "未命名问题")}</h3>
          <p class="finding-meta">
            <span class="badge severity-badge severity-{severity}">{_escape(severity)}</span>
            <span class="badge layer-badge {layer_label.lower()}">{_escape(layer_label)}</span>
            <span class="badge">{_escape(item.get("status") or "open")}</span>
            {reviewer_badge}
            <code>{_escape(_finding_location(item))}</code>
          </p>
          <p>{_escape(item.get("detail") or "")}</p>
          {f"<pre>{_escape(item.get('suggestion'))}</pre>" if item.get("suggestion") else ""}
          {_render_finding_feedback_actions(item) if include_feedback_actions else ""}
        </article>
        """
    )


def _render_finding_feedback_actions(item: Dict[str, Any]) -> str:
    finding_id = item.get("finding_id")
    if not finding_id:
        return ""
    status = str(item.get("status") or "open")
    if status != "open":
        return f'<p class="muted">该问题当前状态为 {_escape(status)}，无需继续处理。</p>'
    escaped_id = _escape(finding_id)
    return f"""
    <div class="feedback-actions" data-finding-id="{escaped_id}">
      <textarea id="feedback-input-{escaped_id}" placeholder="补充反馈：例如为什么这是误判、为什么应降级，或希望模型复核哪些上下文。"></textarea>
      <div class="feedback-buttons">
        <button class="submit-btn" type="button" onclick="submitFindingFeedback('{escaped_id}')">反馈复审</button>
        <button class="mark-btn" type="button" onclick="markHumanNonFix('{escaped_id}')">人工标记非修复</button>
      </div>
      <div class="feedback-result muted" id="feedback-result-{escaped_id}"></div>
    </div>
    """


def _duration_label(seconds: Optional[float]) -> str:
    if seconds is None:
        return "暂无"
    total = max(0, int(round(seconds)))
    minutes, sec = divmod(total, 60)
    hours, minute = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minute}m {sec}s"
    if minute:
        return f"{minute}m {sec}s"
    return f"{sec}s"


def _number_label(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "0"


def _money_label(value: Any) -> str:
    try:
        return f"${float(value):.4f}"
    except (TypeError, ValueError):
        return "$0.0000"


def _compact_token_label(value: Any) -> str:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = 0
    if number >= 1_000_000:
        return f"{number / 1_000_000:.1f}M"
    if number >= 1_000:
        return f"{number / 1_000:.1f}K"
    return str(number)


def _chip_class(name: str) -> str:
    if name in {"passed", "success"}:
        return "ok"
    if name in {"failed", "fatal", "high"}:
        return "bad"
    if name in {"queued", "medium"}:
        return "warn"
    if name == "running":
        return "run"
    return ""


def _render_count_chips(counts: Dict[str, int], empty_text: str) -> str:
    if not counts:
        return f'<span class="chip">{_escape(empty_text)}</span>'
    return "\n".join(
        f'<span class="chip {_chip_class(name)}">{_escape(name)}={count}</span>'
        for name, count in counts.items()
    )


def _report_status_label(data: Dict[str, Any]) -> str:
    if data.get("pass_check"):
        return "通过"
    if data.get("gate_status") in {"failed", "incomplete"}:
        return "未通过"
    return str(data.get("status") or "未知")


def _report_summary_text(data: Dict[str, Any]) -> str:
    findings_count = len(data.get("findings") or [])
    session_count = len(data.get("review_sessions") or [])
    if findings_count == 0:
        return f"本次 CR v2 审查由 {session_count} 个 reviewer session 完成，未发现阻断发布的问题。"
    severity_counts = data.get("severity_counts") or {}
    severity_text = "、".join(f"{name} {count} 个" for name, count in severity_counts.items()) or f"{findings_count} 个"
    return f"本次 CR v2 审查由 {session_count} 个 reviewer session 完成，发现 {severity_text}问题，建议处理后再继续发布。"


def _render_report_meta_grid(data: Dict[str, Any]) -> str:
    items = [
        ("任务ID", data.get("task_id") or "-"),
        ("应用", data.get("app_name") or "-"),
        ("分支", data.get("branch") or "-"),
        ("提交", data.get("commit_id") or "无 commit"),
        ("报告生成时间", _iso_to_shanghai_label(data.get("finished_at") or data.get("created_at"))),
        ("开始执行时间", _iso_to_shanghai_label(data.get("started_at"))),
        ("耗时", _duration_label(data.get("duration_seconds"))),
        ("触发来源", data.get("trigger_source") or "-"),
        ("状态", _report_status_label(data)),
        ("评分", data.get("score") if data.get("score") is not None else "-"),
        ("回调状态", data.get("callback_state") or "暂无"),
        ("问题数", len(data.get("findings") or [])),
    ]
    return "\n".join(
        f"""
        <div class="meta-item">
          <dt>{_escape(label)}</dt>
          <dd>{_escape(value)}</dd>
        </div>
        """
        for label, value in items
    )


def _render_token_usage_summary(usage: Dict[str, Any]) -> str:
    return f"""
    <div class="usage-line">
      <strong>{_escape(_money_label(usage.get("cost_usd")))}</strong>
      <span>{_escape(_compact_token_label(usage.get("total_tokens")))} tokens</span>
      <span class="muted">input {_escape(_number_label(usage.get("input_tokens")))}</span>
      <span class="muted">cache-read {_escape(_number_label(usage.get("cache_read_tokens")))}</span>
      <span class="muted">output {_escape(_number_label(usage.get("output_tokens")))}</span>
      <span class="muted">reasoning {_escape(_number_label(usage.get("reasoning_tokens")))}</span>
    </div>
    """


def _render_report_review_sessions(sessions: List[Dict[str, Any]]) -> str:
    if not sessions:
        return '<p class="muted">暂无 reviewer session。</p>'
    rows = "\n".join(
        f"""
        <tr>
          <td>{_escape(item.get("reviewer") or "-")}</td>
          <td><span class="status {_escape(item.get("status") or "")}">{_escape(item.get("status") or "-")}</span></td>
          <td><code>{_escape(item.get("session_id") or "暂无")}</code></td>
          <td>{_escape(_compact_token_label(item.get("total_tokens")))} tokens</td>
          <td>{_escape(_money_label(item.get("cost_usd")))}</td>
          <td>{_escape(_duration_label(item.get("duration_seconds")))}</td>
          <td>{_escape(_iso_to_shanghai_label(item.get("started_at")))}</td>
          <td>{_escape(_iso_to_shanghai_label(item.get("finished_at")))}</td>
          <td>{_escape(item.get("error") or "")}</td>
        </tr>
        """
        for item in sessions
    )
    return f"""
    <div class="table-wrap">
      <table>
        <thead><tr><th>Reviewer</th><th>Status</th><th>Session</th><th>Tokens</th><th>Cost</th><th>耗时</th><th>Started</th><th>Finished</th><th>Error</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>
    """


def _render_report_feedback_sessions(sessions: List[Dict[str, Any]]) -> str:
    if not sessions:
        return '<p class="muted">暂无反馈复审会话。</p>'
    rows = "\n".join(
        _render_report_feedback_session_row(item)
        for item in sessions
    )
    return f"""
    <div class="table-wrap">
      <table>
        <thead><tr><th>Finding</th><th>Reviewer</th><th>Status</th><th>用户反馈</th><th>模型回复</th><th>Session</th><th>Tokens</th><th>Cost</th><th>Progress</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>
    """


def _render_report_feedback_session_row(item: Dict[str, Any]) -> str:
    task_id = item.get("task_id")
    feedback_session_id = item.get("feedback_session_id")
    progress_link = (
        f'<a href="/reports/{_escape(task_id)}/feedback-sessions/{_escape(feedback_session_id)}/progress" target="_blank" rel="noopener">进度</a>'
        if task_id and feedback_session_id
        else '<span class="muted">暂无</span>'
    )
    return (
        f"""
        <tr>
          <td><code>{_escape(item.get("finding_id") or "general")}</code></td>
          <td>{_escape(item.get("parent_reviewer") or "暂无")}</td>
          <td><span class="status {_escape(item.get("status") or "")}">{_escape(item.get("status") or "-")}</span></td>
          <td>{_escape(item.get("feedback_text") or "暂无")}</td>
          <td>{_escape(item.get("model_reply") or "暂无")}</td>
          <td><code>{_escape(item.get("opencode_session_id") or "暂无")}</code></td>
          <td>{_escape(_compact_token_label(item.get("total_tokens")))} tokens</td>
          <td>{_escape(_money_label(item.get("cost_usd")))}</td>
          <td>{progress_link}</td>
        </tr>
        """
    )


def _render_recent_task_row(item: Dict[str, Any]) -> str:
    task_id = _escape(item["task_id"])
    status = _escape(item["status"])
    gate = _escape(item["gate_status"])
    report_link = (
        f'<a href="{_escape(item["report_url"])}" target="_blank" rel="noopener">报告</a>'
        if item.get("report_url")
        else '<span class="muted">暂无报告</span>'
    )
    ci_bits = [
        f'<div><code>{_escape(item.get("ci_task_id"))}</code></div>' if item.get("ci_task_id") else "",
        f'<div class="muted">record {_escape(item.get("ci_record_id"))}</div>' if item.get("ci_record_id") else "",
    ]
    summary = item.get("summary") or item.get("error_message") or "暂无"
    token_usage = item.get("token_usage") or {}
    token_source = token_usage.get("source") or "usage_log"
    return f"""<tr>
      <td>
        <div class="job-main">
          <strong>{_escape(item["app_name"])}</strong>
          <code>{task_id}</code>
          <span class="branch">{_escape(item["branch"])}</span>
          <span class="muted">{_escape((item.get("commit_id") or "")[:12]) or "无 commit"}</span>
        </div>
      </td>
      <td><span class="status {status}">{status}</span><div class="muted">attempts {item["attempts"]}</div></td>
      <td><span class="status {gate}">{gate}</span></td>
      <td>
        <div>{item["findings_count"]} findings</div>
        <div class="muted">score {item["score"] if item.get("score") is not None else "-"}</div>
      </td>
      <td>
        <div class="token-total">{_escape(_money_label(token_usage.get("cost_usd")))} · {_escape(_compact_token_label(token_usage.get("total_tokens")))} tokens</div>
        <div class="muted">{_escape(token_usage.get("calls") or 0)} calls · {_escape(token_source)}</div>
      </td>
      <td>{"".join(ci_bits) or '<span class="muted">暂无</span>'}</td>
      <td>{'成功' if item["callback_succeeded"] else '<span class="muted">未完成</span>'}</td>
      <td>
        <div>{_escape(_iso_to_shanghai_label(item.get("created_at")))} GMT+8</div>
        <div class="muted">耗时 {_escape(_duration_label(item.get("duration_seconds")))}</div>
      </td>
      <td>
        <div class="links">
          <a href="/task-status/{task_id}" target="_blank" rel="noopener">状态</a>
          {report_link}
        </div>
      </td>
      <td><div class="summary">{_escape(summary)}</div></td>
    </tr>"""


def _render_recent_reports_page(data: Dict[str, Any], query: Dict[str, int]) -> str:
    params = urlencode(query)
    summary = data["summary"]
    rows = "\n".join(_render_recent_task_row(item) for item in data["tasks"])
    if not rows:
        rows = '<tr><td colspan="10" class="empty">当前窗口内没有任务</td></tr>'

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>CR 最近 LLM 任务</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f6f8fb;
      --panel: #ffffff;
      --text: #18212f;
      --muted: #5f6f85;
      --border: #d9e0ea;
      --link: #1769aa;
      --ok: #18794e;
      --bad: #b42318;
      --warn: #9a6700;
      --run: #175cd3;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: "PingFang SC", "Microsoft YaHei", Arial, sans-serif; color: var(--text); background: var(--bg); line-height: 1.5; }}
    main {{ width: min(1360px, calc(100vw - 32px)); margin: 28px auto 40px; }}
    header {{ display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; margin-bottom: 16px; }}
    h1 {{ margin: 0 0 6px; font-size: 28px; line-height: 1.2; letter-spacing: 0; }}
    h2 {{ margin: 0 0 12px; font-size: 18px; letter-spacing: 0; }}
    p {{ margin: 0; }}
    a {{ color: var(--link); text-decoration: none; font-weight: 600; }}
    a:hover {{ text-decoration: underline; }}
    code {{ padding: 2px 6px; border-radius: 4px; background: #eef3f8; color: #233142; word-break: break-word; }}
    .muted {{ color: var(--muted); }}
    .toolbar {{ display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 8px; }}
    .button {{ display: inline-flex; align-items: center; min-height: 36px; padding: 7px 12px; border: 1px solid var(--border); border-radius: 6px; background: var(--panel); color: var(--text); font: inherit; font-weight: 600; cursor: pointer; }}
    .button.primary {{ border-color: var(--link); background: var(--link); color: #fff; }}
    .panel {{ background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 18px; margin-bottom: 16px; }}
    .summary-grid {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }}
    .metric {{ min-height: 86px; border: 1px solid var(--border); border-radius: 8px; padding: 14px; background: #fbfcfe; }}
    .metric-label {{ margin-bottom: 4px; color: var(--muted); font-size: 13px; }}
    .metric-value {{ font-size: 30px; line-height: 1.1; font-weight: 800; letter-spacing: 0; }}
    .chips {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }}
    .chip {{ display: inline-flex; align-items: center; min-height: 28px; padding: 3px 9px; border: 1px solid var(--border); border-radius: 6px; background: #f8fafc; font-size: 13px; white-space: nowrap; }}
    .chip.ok, .status.passed, .status.success {{ color: var(--ok); }}
    .chip.bad, .status.failed {{ color: var(--bad); }}
    .chip.warn, .status.queued {{ color: var(--warn); }}
    .chip.run, .status.running {{ color: var(--run); }}
    .table-wrap {{ overflow-x: auto; border: 1px solid var(--border); border-radius: 8px; }}
    table {{ width: 100%; min-width: 1240px; border-collapse: collapse; background: var(--panel); }}
    th, td {{ padding: 10px 12px; border-bottom: 1px solid var(--border); text-align: left; vertical-align: top; font-size: 14px; }}
    th {{ background: #f1f5f9; color: #344054; font-size: 12px; text-transform: uppercase; letter-spacing: 0; white-space: nowrap; }}
    tbody tr:last-child td {{ border-bottom: 0; }}
    .status {{ display: inline-block; font-weight: 800; white-space: nowrap; }}
    .job-main {{ display: grid; gap: 5px; min-width: 240px; }}
    .branch {{ max-width: 300px; overflow-wrap: anywhere; }}
    .summary {{ max-width: 420px; color: var(--muted); overflow-wrap: anywhere; }}
    .token-total {{ font-weight: 800; white-space: nowrap; }}
    .links {{ display: flex; flex-wrap: wrap; gap: 8px; min-width: 130px; }}
    .empty {{ padding: 30px; text-align: center; color: var(--muted); }}
    @media (max-width: 860px) {{
      main {{ width: min(100vw - 24px, 1360px); margin-top: 18px; }}
      header {{ display: block; }}
      .toolbar {{ justify-content: flex-start; margin-top: 12px; }}
      .summary-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    }}
    @media (max-width: 520px) {{
      .summary-grid {{ grid-template-columns: 1fr; }}
      h1 {{ font-size: 24px; }}
    }}
  </style>
</head>
<body>
<main>
  <header>
    <div>
      <h1>CR 最近 LLM 任务</h1>
      <p class="muted">窗口: {_escape(_iso_to_shanghai_label(data["window_start"]))} GMT+8 至 {_escape(_iso_to_shanghai_label(data["window_end"]))} GMT+8，最近 {data["hours"]} 小时。</p>
    </div>
    <div class="toolbar" aria-label="页面操作">
      <a class="button" href="/reports/recent/data?{params}">JSON</a>
      <a class="button primary" href="/reports/recent.html?{params}">刷新</a>
    </div>
  </header>

  <section class="panel" aria-labelledby="summary-title">
    <h2 id="summary-title">汇总</h2>
    <div class="summary-grid">
      <div class="metric"><div class="metric-label">任务数</div><div class="metric-value">{summary["total"]}</div></div>
      <div class="metric"><div class="metric-label">通过</div><div class="metric-value">{summary["passed"]}</div></div>
      <div class="metric"><div class="metric-label">未通过/失败</div><div class="metric-value">{summary["failed"]}</div></div>
      <div class="metric"><div class="metric-label">运行中/排队</div><div class="metric-value">{summary["running_or_queued"]}</div></div>
    </div>
    <div class="chips" aria-label="Token 汇总"><span class="chip">tokens={_escape(_number_label(summary.get("tokens")))}</span></div>
    <div class="chips" aria-label="门禁分布">{_render_count_chips(data["gate_counts"], "暂无门禁状态")}</div>
    <div class="chips" aria-label="应用分布">{_render_count_chips(data["app_counts"], "暂无应用")}</div>
    <div class="chips" aria-label="问题级别分布">{_render_count_chips(data["severity_counts"], "暂无问题")}</div>
  </section>

  <section class="panel" aria-labelledby="jobs-title">
    <h2 id="jobs-title">任务明细</h2>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>任务</th>
            <th>状态</th>
            <th>门禁</th>
            <th>问题/分数</th>
            <th>Token</th>
            <th>CI</th>
            <th>回调</th>
            <th>时间</th>
            <th>链接</th>
            <th>摘要</th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
    </div>
  </section>
</main>
</body>
</html>"""


def _get_false_positive_sync_state() -> Dict[str, Any]:
    with _sync_lock:
        return dict(_sync_state)


@router.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@router.get("/healthcheck.html")
def healthcheck_html() -> FileResponse:
    return FileResponse(_TEMPLATE_DIR / "healthcheck.html", media_type="text/html; charset=utf-8")


@router.get("/task-status/{task_id}")
def task_status_page(task_id: str, service: TaskService = Depends(get_task_service)) -> HTMLResponse:
    if service.settings.review_v2_enabled:
        data = ReviewV2Views(service.review_v2_db).task_status(task_id)
        if not data:
            raise HTTPException(status_code=404, detail="task not found")
        initial_status = data["status"]
        initial_score = data["result"]["score"] if data.get("result") else "-"
        initial_summary = data.get("result", {}).get("summary") or "暂无"
        initial_error = data.get("error_message") or "暂无"
        initial_report_url = data.get("report_url") or ""
        initial_progress_url = f"/reports/{task_id}/progress"
        initial_started_at = _iso_to_shanghai_label(data.get("started_at"))
        should_poll = initial_status in {"queued", "running"}
    else:
        record = service.get(task_id)
        if record is None:
            raise HTTPException(status_code=404, detail="task not found")
        initial_status = record.status.value
        initial_score = record.result.score if record.result is not None else "-"
        initial_summary = record.result.summary if record.result is not None else "暂无"
        initial_error = record.error_message or "暂无"
        initial_report_url = record.report_url or ""
        initial_progress_url = ""
        initial_started_at = _to_shanghai_label(record.started_at)
        should_poll = record.status.value in {"queued", "running"}
    report_link_html = (
        f'<a id="report" href="{_escape(initial_report_url)}" target="_blank" rel="noopener">打开报告</a>'
        '<span id="report-empty" class="muted" style="display:none">暂无</span>'
        if initial_report_url
        else '<span id="report-empty" class="muted">暂无</span><a id="report" href="#" target="_blank" rel="noopener" style="display:none">打开报告</a>'
    )
    progress_link_html = (
        f'<a href="{_escape(initial_progress_url)}" target="_blank" rel="noopener">打开进度</a>'
        if initial_progress_url
        else '<span class="muted">暂无</span>'
    )
    review_logic_link_html = (
        f'<a href="{_escape(CR_V2_LOGIC_DOC_URL)}" target="_blank" rel="noopener">了解 CR v2 审查逻辑</a>'
        if service.settings.review_v2_enabled
        else '<span class="muted">暂无</span>'
    )
    html = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>任务状态 {_escape(task_id)}</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 32px; color: #111; }}
    .muted {{ color: #666; }}
    .card {{ max-width: 960px; padding: 24px; border: 1px solid #ddd; border-radius: 12px; }}
    pre {{ white-space: pre-wrap; word-break: break-word; background: #f7f7f7; padding: 12px; border-radius: 8px; }}
    a {{ color: #0b57d0; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>代码扫描任务状态</h1>
    <p class="muted">task_id: {_escape(task_id)}</p>
    <p>当前状态: <strong id="status">{_escape(initial_status)}</strong></p>
    <p>开始执行时间: <span id="started-at">{_escape(initial_started_at)}</span></p>
    <p>分数: <strong id="score">{_escape(initial_score)}</strong></p>
    <p>报告: {report_link_html}</p>
    <p>进度: {progress_link_html}</p>
    <p>审查逻辑: {review_logic_link_html}</p>
    <p>错误: <span id="error" class="muted">{_escape(initial_error)}</span></p>
    <h3>摘要</h3>
    <pre id="summary">{_escape(initial_summary)}</pre>
  </div>
  <script>
    const taskId = {task_id!r};
    function esc(value) {{
      return String(value == null ? '' : value).replace(/[&<>"']/g, ch => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[ch]));
    }}
    function toShanghaiLabel(isoText) {{
      if (!isoText) return '暂无';
      return new Intl.DateTimeFormat('zh-CN', {{
        timeZone: 'Asia/Shanghai',
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
        hour12: false,
      }}).format(new Date(isoText));
    }}
    async function refresh() {{
      const resp = await fetch(`/task-status/${{taskId}}/data`, {{ cache: 'no-store' }});
      const data = await resp.json();
      document.getElementById('status').textContent = data.status || '-';
      document.getElementById('started-at').textContent = toShanghaiLabel(data.started_at);
      document.getElementById('score').textContent = data.result && data.result.score != null ? data.result.score : '-';
      document.getElementById('summary').textContent = data.result && data.result.summary ? data.result.summary : '暂无';
      document.getElementById('error').textContent = data.error_message || '暂无';
      const report = document.getElementById('report');
      const reportEmpty = document.getElementById('report-empty');
      if (data.report_url) {{
        report.href = data.report_url;
        report.style.display = '';
        reportEmpty.style.display = 'none';
      }} else {{
        report.style.display = 'none';
        reportEmpty.style.display = '';
      }}
      if (data.status === 'queued' || data.status === 'running') {{
        setTimeout(refresh, 3000);
      }}
    }}
    if ({str(should_poll).lower()}) {{
      refresh();
    }}
  </script>
</body>
</html>"""
    return HTMLResponse(content=html)


@router.get("/task-status/{task_id}/data")
def task_status_data(task_id: str, service: TaskService = Depends(get_task_service)) -> Any:
    if service.settings.review_v2_enabled:
        data = ReviewV2Views(service.review_v2_db).task_status(task_id)
        if not data:
            raise HTTPException(status_code=404, detail="task not found")
        return data
    record = service.get(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail="task not found")
    return record


def _render_progress_page(task_id: str, data: Dict[str, Any]) -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>CR 进度 {_escape(task_id)}</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 32px; color: #111; background:#f7f9fc; }}
    .wrap {{ max-width: 1100px; margin: 0 auto; }}
    .panel {{ background:#fff; border:1px solid #d9e1ec; border-radius:10px; padding:18px; margin-bottom:16px; }}
    .muted {{ color:#667085; }}
    .grid {{ display:grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap:12px; }}
    table {{ width:100%; border-collapse:collapse; }}
    th, td {{ text-align:left; border-bottom:1px solid #edf1f7; padding:8px; vertical-align:top; }}
    th {{ color:#667085; font-size:12px; }}
    code {{ background:#f1f5f9; padding:2px 5px; border-radius:4px; }}
    .pill {{ display:inline-block; border:1px solid #d0d7e2; border-radius:999px; padding:2px 8px; font-size:12px; background:#f8fafc; }}
  </style>
</head>
<body>
<div class="wrap">
  <div class="panel">
    <h1>CR 任务进度</h1>
    <p class="muted">task_id: <code>{_escape(task_id)}</code></p>
    <div class="grid">
      <div>状态<br><strong id="status">{_escape(data.get("status") or "-")}</strong></div>
      <div>Gate<br><strong id="gate">{_escape(data.get("gate_status") or "-")}</strong></div>
      <div>审查模式<br><strong id="mode">{_escape(data.get("review_mode") or "-")}</strong></div>
      <div>当前阶段<br><strong id="stage">{_escape(data.get("current_stage") or "-")}</strong></div>
      <div>Token/Cost<br><strong id="usage">-</strong></div>
    </div>
    <p id="detail" class="muted">{_escape(data.get("current_detail") or "")}</p>
  </div>
  <div class="panel">
    <h2>Reviewer Sessions</h2>
    <table><thead><tr><th>Reviewer</th><th>Model</th><th>Required</th><th>Status</th><th>Session</th><th>Tokens</th><th>Cost</th><th>Started</th><th>Finished</th><th>Error / Reason</th></tr></thead><tbody id="reviewers"></tbody></table>
  </div>
  <div class="panel">
    <h2>Feedback Sessions</h2>
    <table><thead><tr><th>Finding</th><th>Reviewer</th><th>Status</th><th>模型回复</th><th>Session</th><th>Tokens</th><th>Cost</th><th>Progress</th></tr></thead><tbody id="feedback"></tbody></table>
  </div>
  <div class="panel">
    <h2>Timeline</h2>
    <table><thead><tr><th>Time</th><th>Stage</th><th>Event</th><th>Message</th></tr></thead><tbody id="events"></tbody></table>
  </div>
</div>
<script>
const taskId = {task_id!r};
function esc(value) {{
  return String(value == null ? '' : value).replace(/[&<>"']/g, ch => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[ch]));
}}
function time(value) {{
  if (!value) return '-';
  return new Intl.DateTimeFormat('zh-CN', {{ timeZone:'Asia/Shanghai', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', second:'2-digit', hour12:false }}).format(new Date(value));
}}
function compactTokens(value) {{
  const n = Number(value || 0);
  if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
  if (n >= 1000) return (n / 1000).toFixed(1) + 'K';
  return String(n);
}}
function money(value) {{
  return '$' + Number(value || 0).toFixed(4);
}}
async function refresh() {{
  const data = await fetch(`/reports/${{taskId}}/progress/data`, {{ cache:'no-store' }}).then(r => r.json());
  document.getElementById('status').textContent = data.status || '-';
  document.getElementById('gate').textContent = data.gate_status || '-';
  document.getElementById('mode').textContent = data.review_mode || '-';
  document.getElementById('stage').textContent = data.current_stage || '-';
  document.getElementById('detail').textContent = data.current_detail || '';
  const usage = data.token_usage || {{}};
  document.getElementById('usage').textContent = `${{money(usage.cost_usd)}} · ${{compactTokens(usage.total_tokens)}} tokens`;
  document.getElementById('reviewers').innerHTML = (data.review_sessions || []).map(item => `
    <tr>
      <td>${{esc(item.reviewer)}}</td>
      <td><code>${{esc(item.model_id || '-')}}</code></td>
      <td>${{item.required ? 'required' : 'optional'}}</td>
      <td><span class="pill">${{esc(item.status)}}</span></td>
      <td><code>${{esc(item.session_id || '-')}}</code></td>
      <td>${{compactTokens(item.total_tokens)}} </td>
      <td>${{money(item.cost_usd)}}</td>
      <td>${{time(item.started_at)}}</td>
      <td>${{time(item.finished_at)}}</td>
      <td>${{esc(item.error || item.reason || '')}}</td>
    </tr>`).join('');
  document.getElementById('feedback').innerHTML = (data.feedback_sessions || []).map(item => `
    <tr>
      <td><code>${{esc(item.finding_id || '-')}}</code></td>
      <td>${{esc(item.parent_reviewer || '-')}}</td>
      <td><span class="pill">${{esc(item.status)}}</span></td>
      <td>${{esc(item.model_reply || '-')}}</td>
      <td><code>${{esc(item.opencode_session_id || '-')}}</code></td>
      <td>${{compactTokens(item.total_tokens)}} </td>
      <td>${{money(item.cost_usd)}}</td>
      <td><a href="/reports/${{encodeURIComponent(taskId)}}/feedback-sessions/${{encodeURIComponent(item.feedback_session_id || '')}}/progress" target="_blank" rel="noopener">进度</a></td>
    </tr>`).join('');
  document.getElementById('events').innerHTML = (data.events || []).slice().reverse().map(item => `
    <tr>
      <td>${{time(item.created_at)}}</td>
      <td>${{esc(item.stage || '')}}</td>
      <td>${{esc(item.event_type || '')}}</td>
      <td>${{esc(item.message || '')}}</td>
    </tr>`).join('');
  if (data.status === 'queued' || data.status === 'running') setTimeout(refresh, 3000);
}}
refresh();
</script>
</body>
</html>"""


def _render_feedback_progress_page(task_id: str, feedback_session_id: str, data: Dict[str, Any]) -> str:
    finding = data.get("finding") or {}
    task = data.get("task") or {}
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Feedback 复审进度 {_escape(feedback_session_id)}</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin:32px; color:#111; background:#f7f9fc; }}
    .wrap {{ max-width: 1100px; margin:0 auto; }}
    .panel {{ background:#fff; border:1px solid #d9e1ec; border-radius:10px; padding:18px; margin-bottom:16px; }}
    .muted {{ color:#667085; }}
    .grid {{ display:grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap:12px; }}
    table {{ width:100%; border-collapse:collapse; }}
    th, td {{ text-align:left; border-bottom:1px solid #edf1f7; padding:8px; vertical-align:top; }}
    th {{ color:#667085; font-size:12px; }}
    code {{ background:#f1f5f9; padding:2px 5px; border-radius:4px; word-break:break-all; }}
    .pill {{ display:inline-block; border:1px solid #d0d7e2; border-radius:999px; padding:2px 8px; font-size:12px; background:#f8fafc; }}
    pre {{ white-space:pre-wrap; word-break:break-word; background:#f7f7f7; border-radius:8px; padding:12px; }}
  </style>
</head>
<body>
<div class="wrap">
  <div class="panel">
    <h1>Feedback 复审进度</h1>
    <p class="muted">task_id: <code>{_escape(task_id)}</code> · feedback_session_id: <code>{_escape(feedback_session_id)}</code></p>
    <div class="grid">
      <div>状态<br><strong id="status">{_escape(data.get("status") or "-")}</strong></div>
      <div>Reviewer<br><strong id="reviewer">{_escape(data.get("parent_reviewer") or "-")}</strong></div>
      <div>OpenCode Session<br><code id="session">{_escape(data.get("opencode_session_id") or "-")}</code></div>
      <div>Token/Cost<br><strong id="usage">-</strong></div>
      <div>创建时间<br><strong id="created">{_escape(_iso_to_shanghai_label(data.get("created_at")))}</strong></div>
      <div>更新时间<br><strong id="updated">{_escape(_iso_to_shanghai_label(data.get("updated_at")))}</strong></div>
    </div>
    <p><a href="/reports/{_escape(task_id)}/progress" target="_blank" rel="noopener">父任务进度</a> · <a href="{_escape(task.get("report_url") or f"/reports/{task_id}/index.html")}" target="_blank" rel="noopener">报告</a></p>
  </div>
  <div class="panel">
    <h2>Finding</h2>
    <p><code id="finding-id">{_escape(data.get("finding_id") or "-")}</code></p>
    <p><strong id="finding-title">{_escape(finding.get("title") or "-")}</strong></p>
    <p id="finding-location"><code>{_escape(finding.get("file") or "-")}</code>{":" + _escape(finding.get("line")) if finding.get("line") else ""}</p>
    <pre id="finding-detail">{_escape(finding.get("detail") or "")}</pre>
  </div>
  <div class="panel">
    <h2>用户反馈</h2>
    <pre id="feedback-text">{_escape(data.get("feedback_text") or "暂无")}</pre>
  </div>
  <div class="panel">
    <h2>模型回复</h2>
    <pre id="model-reply">{_escape(data.get("model_reply") or "暂无")}</pre>
  </div>
  <div class="panel">
    <h2>Timeline</h2>
    <table><thead><tr><th>Time</th><th>Event</th><th>Message</th></tr></thead><tbody id="events"></tbody></table>
  </div>
</div>
<script>
const taskId = {task_id!r};
const feedbackSessionId = {feedback_session_id!r};
function esc(value) {{
  return String(value == null ? '' : value).replace(/[&<>"']/g, ch => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[ch]));
}}
function time(value) {{
  if (!value) return '-';
  return new Intl.DateTimeFormat('zh-CN', {{ timeZone:'Asia/Shanghai', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', second:'2-digit', hour12:false }}).format(new Date(value));
}}
function compactTokens(value) {{
  const n = Number(value || 0);
  if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
  if (n >= 1000) return (n / 1000).toFixed(1) + 'K';
  return String(n);
}}
function money(value) {{ return '$' + Number(value || 0).toFixed(4); }}
async function refresh() {{
  const data = await fetch(`/reports/${{encodeURIComponent(taskId)}}/feedback-sessions/${{encodeURIComponent(feedbackSessionId)}}/progress/data`, {{ cache:'no-store' }}).then(r => r.json());
  document.getElementById('status').textContent = data.status || '-';
  document.getElementById('reviewer').textContent = data.parent_reviewer || '-';
  document.getElementById('session').textContent = data.opencode_session_id || '-';
  document.getElementById('created').textContent = time(data.created_at);
  document.getElementById('updated').textContent = time(data.updated_at);
  const usage = data.token_usage || {{}};
  document.getElementById('usage').textContent = `${{money(usage.cost_usd)}} · ${{compactTokens(usage.total_tokens)}} tokens`;
  document.getElementById('feedback-text').textContent = data.feedback_text || '暂无';
  document.getElementById('model-reply').textContent = data.model_reply || '暂无';
  document.getElementById('events').innerHTML = (data.events || []).slice().reverse().map(item => `
    <tr>
      <td>${{time(item.created_at)}}</td>
      <td><span class="pill">${{esc(item.event_type || '')}}</span></td>
      <td>${{esc(item.message || '')}}</td>
    </tr>`).join('');
  if (data.status === 'running') setTimeout(refresh, 3000);
}}
refresh();
</script>
</body>
</html>"""


@router.get("/reports/{task_id}/progress/data")
def report_progress_data(task_id: str, service: TaskService = Depends(get_task_service)) -> Dict[str, Any]:
    if not service.settings.review_v2_enabled:
        raise HTTPException(status_code=404, detail="progress is only available for review v2")
    data = ReviewV2Views(service.review_v2_db).progress_report(task_id)
    if not data:
        raise HTTPException(status_code=404, detail="task not found")
    return data


@router.get("/reports/{task_id}/progress")
def report_progress_page(task_id: str, service: TaskService = Depends(get_task_service)) -> HTMLResponse:
    if not service.settings.review_v2_enabled:
        raise HTTPException(status_code=404, detail="progress is only available for review v2")
    data = ReviewV2Views(service.review_v2_db).progress_report(task_id)
    if not data:
        raise HTTPException(status_code=404, detail="task not found")
    return HTMLResponse(content=_render_progress_page(task_id, data))


@router.get("/reports/{task_id}/feedback-sessions/{feedback_session_id}/progress/data")
def feedback_progress_data(
    task_id: str,
    feedback_session_id: str,
    service: TaskService = Depends(get_task_service),
) -> Dict[str, Any]:
    if not service.settings.review_v2_enabled:
        raise HTTPException(status_code=404, detail="feedback progress is only available for review v2")
    data = ReviewV2Views(service.review_v2_db).feedback_progress(task_id, feedback_session_id)
    if not data:
        raise HTTPException(status_code=404, detail="feedback session not found")
    return data


@router.get("/reports/{task_id}/feedback-sessions/{feedback_session_id}/progress")
def feedback_progress_page(
    task_id: str,
    feedback_session_id: str,
    service: TaskService = Depends(get_task_service),
) -> HTMLResponse:
    if not service.settings.review_v2_enabled:
        raise HTTPException(status_code=404, detail="feedback progress is only available for review v2")
    data = ReviewV2Views(service.review_v2_db).feedback_progress(task_id, feedback_session_id)
    if not data:
        raise HTTPException(status_code=404, detail="feedback session not found")
    return HTMLResponse(content=_render_feedback_progress_page(task_id, feedback_session_id, data))


def _render_review_report_page(data: Dict[str, Any]) -> str:
    findings = data.get("findings") or []
    findings_html = _render_findings_html(findings, include_feedback_actions=True)
    app_name = data.get("app_name") or "CR"
    usage = data.get("token_usage") or {}
    summary_text = _report_summary_text(data)
    has_running_feedback = any(item.get("status") == "running" for item in data.get("feedback_sessions") or [])
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_escape(app_name)} 大模型扫描报告</title>
  <style>
    :root {{ color-scheme: light; --text:#172033; --muted:#667085; --border:#d9e1ec; --soft:#f6f8fb; --panel:#fff; --ok:#15803d; --bad:#b91c1c; --warn:#b45309; --blue:#2563eb; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 0; color: var(--text); background: var(--soft); }}
    .wrap {{ max-width: 1160px; margin: 0 auto; padding: 28px 20px 48px; }}
    .topbar {{ display:flex; align-items:center; justify-content:space-between; gap:16px; margin-bottom:18px; }}
    h1 {{ margin:0; font-size:28px; line-height:1.2; letter-spacing:0; }}
    h2 {{ margin:28px 0 12px; font-size:19px; letter-spacing:0; }}
    h3 {{ margin:0 0 8px; font-size:16px; letter-spacing:0; }}
    .muted, .finding-meta {{ color:#667085; }}
    .panel {{ background:var(--panel); border:1px solid var(--border); border-radius:8px; padding:18px; margin:14px 0; }}
    .meta-grid {{ display:grid; grid-template-columns:repeat(4, minmax(0,1fr)); gap:12px; }}
    .meta-item {{ min-width:0; border-top:1px solid #edf1f7; padding-top:10px; }}
    .meta-item dt {{ color:var(--muted); font-size:12px; margin-bottom:4px; }}
    .meta-item dd {{ margin:0; font-weight:700; overflow-wrap:anywhere; }}
    .score {{ display:flex; align-items:baseline; gap:10px; margin-top:12px; }}
    .score strong {{ font-size:34px; }}
    .actions {{ display:flex; gap:10px; flex-wrap:wrap; }}
    .actions a {{ display:inline-flex; align-items:center; text-decoration:none; color:#0f172a; border:1px solid var(--border); border-radius:8px; padding:8px 10px; background:#fff; }}
    .badge {{ display:inline-block; border:1px solid #d0d7e2; border-radius:999px; padding:1px 8px; margin-right:6px; font-size:12px; }}
    .severity-group {{ border:1px solid var(--border); border-radius:8px; padding:14px; margin:12px 0 18px; background:#fff; }}
    .severity-group > h3 {{ display:flex; align-items:center; gap:8px; margin-bottom:10px; }}
    .severity-group.severity-fatal {{ border-left:5px solid #991b1b; background:#fff7f7; }}
    .severity-group.severity-high {{ border-left:5px solid #dc2626; background:#fff8f8; }}
    .severity-group.severity-medium {{ border-left:5px solid #d97706; background:#fffbeb; }}
    .severity-group.severity-low {{ border-left:5px solid #2563eb; background:#f8fbff; }}
    .severity-group.severity-info {{ border-left:5px solid #64748b; background:#f8fafc; }}
    .finding {{ border:1px solid var(--border); border-radius:8px; padding:14px; margin:12px 0; background:#fff; }}
    .severity-badge.severity-fatal {{ background:#7f1d1d; border-color:#7f1d1d; color:#fff; }}
    .severity-badge.severity-high {{ background:#dc2626; border-color:#dc2626; color:#fff; }}
    .severity-badge.severity-medium {{ background:#f59e0b; border-color:#f59e0b; color:#422006; }}
    .severity-badge.severity-low {{ background:#dbeafe; border-color:#93c5fd; color:#1d4ed8; }}
    .severity-badge.severity-info {{ background:#e2e8f0; border-color:#cbd5e1; color:#334155; }}
    .layer-badge.blocking {{ background:#fee2e2; border-color:#fecaca; color:#991b1b; }}
    .layer-badge.non-blocking {{ background:#ecfdf5; border-color:#bbf7d0; color:#047857; }}
    .feedback-actions {{ margin-top:12px; border-top:1px solid #edf1f7; padding-top:12px; }}
    .feedback-buttons {{ display:flex; gap:8px; flex-wrap:wrap; margin-top:8px; }}
    .feedback-result {{ margin-top:8px; }}
    textarea {{ width:100%; min-height:76px; box-sizing:border-box; border:1px solid #cbd5e1; border-radius:8px; padding:10px; font:inherit; resize:vertical; }}
    button {{ border:0; border-radius:8px; padding:8px 12px; cursor:pointer; }}
    .submit-btn {{ background:var(--blue); color:#fff; }}
    .mark-btn {{ background:#e2e8f0; color:#0f172a; }}
    pre {{ white-space:pre-wrap; word-break:break-word; background:#f7f7f7; padding:12px; border-radius:8px; }}
    code {{ word-break:break-all; }}
    .summary-text {{ white-space:pre-wrap; line-height:1.65; }}
    .usage-line {{ display:flex; flex-wrap:wrap; gap:10px 14px; align-items:center; }}
    .table-wrap {{ overflow-x:auto; }}
    table {{ width:100%; border-collapse:collapse; font-size:13px; }}
    th, td {{ text-align:left; border-bottom:1px solid #edf1f7; padding:9px 8px; vertical-align:top; }}
    th {{ color:#475467; font-weight:700; background:#f9fafb; }}
    .status {{ display:inline-block; border-radius:999px; padding:2px 8px; background:#eef2f7; font-size:12px; font-weight:700; }}
    .status.success, .status.passed, .status.skipped {{ background:#dcfce7; color:var(--ok); }}
    .status.failed, .status.incomplete, .status.high, .status.fatal {{ background:#fee2e2; color:var(--bad); }}
    .status.running, .status.queued, .status.medium {{ background:#fef3c7; color:var(--warn); }}
    @media (max-width: 820px) {{
      .wrap {{ padding:20px 12px 36px; }}
      .topbar {{ align-items:flex-start; flex-direction:column; }}
      .meta-grid {{ grid-template-columns:1fr 1fr; }}
    }}
    @media (max-width: 520px) {{
      .meta-grid {{ grid-template-columns:1fr; }}
      h1 {{ font-size:23px; }}
    }}
  </style>
</head>
<body>
  <main class="wrap">
    <div class="topbar">
      <div>
        <h1>{_escape(app_name)} 大模型扫描报告</h1>
      </div>
      <nav class="actions" aria-label="report actions">
        <a href="/task-status/{_escape(data.get("task_id"))}" target="_blank" rel="noopener">任务状态</a>
        <a href="/reports/{_escape(data.get("task_id"))}/progress" target="_blank" rel="noopener">执行进度</a>
        <a href="/reports/{_escape(data.get("task_id"))}/detail" target="_blank" rel="noopener">JSON</a>
      </nav>
    </div>

    <section class="panel" aria-labelledby="report-meta-title">
      <h2 id="report-meta-title">报告信息</h2>
      <dl class="meta-grid">
        {_render_report_meta_grid(data)}
      </dl>
      <div class="score">
        <span class="muted">评分</span>
        <strong>{_escape(data.get("score") if data.get("score") is not None else "-")}</strong>
        <span class="status {_escape(data.get("gate_status") or "")}">{_escape(_report_status_label(data))}</span>
      </div>
    </section>

    <section class="panel" aria-labelledby="summary-title">
      <h2 id="summary-title">摘要</h2>
      <p class="summary-text">{_escape(summary_text)}</p>
      {_render_token_usage_summary(usage)}
    </section>

    <h2>问题列表 ({len(findings)})</h2>
    {findings_html}

    <section class="panel" aria-labelledby="reviewer-title">
      <h2 id="reviewer-title">审查会话</h2>
      {_render_report_review_sessions(data.get("review_sessions") or [])}
    </section>

    <section class="panel" aria-labelledby="feedback-title">
      <h2 id="feedback-title">其他反馈（漏判）</h2>
      <p class="muted">CR v2 当前主要支持针对 finding 的反馈复审和人工非修复标记；通用漏判反馈入口暂未启用。</p>
      {_render_report_feedback_sessions(data.get("feedback_sessions") or [])}
    </section>

    <section class="panel" aria-labelledby="fix-title">
      <h2 id="fix-title">一键修复</h2>
      <p class="muted">CR v2 当前为只读审查，不在报告页内自动修改代码。请在代码仓库修复后重新触发 CR。</p>
    </section>
  </main>
  <script>
    const taskId = {_json_for_script(data.get("task_id"))};
    const hasRunningFeedback = {str(has_running_feedback).lower()};
    function setFeedbackResult(findingId, message, isError = false) {{
      const target = document.getElementById(`feedback-result-${{findingId}}`);
      if (!target) return;
      target.textContent = message;
      target.style.color = isError ? '#b91c1c' : '#15803d';
    }}
    async function postFindingFeedback(findingId, payload) {{
      const resp = await fetch(`/reports/${{encodeURIComponent(taskId)}}/findings/by-id/${{encodeURIComponent(findingId)}}/feedback`, {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify(payload),
      }});
      const text = await resp.text();
      let data = {{}};
      try {{ data = text ? JSON.parse(text) : {{}}; }} catch (error) {{ data = {{ message: text }}; }}
      if (!resp.ok) {{
        throw new Error(data.detail || data.message || text || '提交失败');
      }}
      return data;
    }}
    async function submitFindingFeedback(findingId) {{
      const input = document.getElementById(`feedback-input-${{findingId}}`);
      const message = (input && input.value ? input.value : '').trim();
      if (!message) {{
        setFeedbackResult(findingId, '请先填写反馈内容。', true);
        return;
      }}
      setFeedbackResult(findingId, '正在提交反馈复审...');
      try {{
        const data = await postFindingFeedback(findingId, {{ message }});
        if (input) input.value = '';
        setFeedbackResult(findingId, data.message || '已提交反馈复审');
        setTimeout(() => window.location.reload(), 600);
      }} catch (error) {{
        setFeedbackResult(findingId, error.message || '提交失败', true);
      }}
    }}
    async function markHumanNonFix(findingId) {{
      const input = document.getElementById(`feedback-input-${{findingId}}`);
      const rationale = (input && input.value ? input.value : '').trim();
      if (!rationale) {{
        setFeedbackResult(findingId, '请先填写人工判定理由。', true);
        return;
      }}
      setFeedbackResult(findingId, '正在标记...');
      try {{
        const data = await postFindingFeedback(findingId, {{
          disposition: 'human_non_fix',
          rationale,
          actor: 'human',
        }});
        setFeedbackResult(findingId, data.message || '已标记为人工非修复');
        setTimeout(() => window.location.reload(), 800);
      }} catch (error) {{
        setFeedbackResult(findingId, error.message || '提交失败', true);
      }}
    }}
    if (hasRunningFeedback) {{
      setTimeout(() => window.location.reload(), 3000);
    }}
  </script>
</body>
</html>"""


@router.get("/reports/{task_id}/index.html", response_model=None)
def report_index_page(task_id: str, service: TaskService = Depends(get_task_service)) -> Any:
    if service.settings.review_v2_enabled:
        data = ReviewV2Views(service.review_v2_db).report_detail(task_id)
        if data:
            return HTMLResponse(content=_render_review_report_page(data))
    static_report = service.settings.report_dir / task_id / "index.html"
    if static_report.exists():
        return FileResponse(static_report, media_type="text/html; charset=utf-8")
    raise HTTPException(status_code=404, detail="report not found")


@router.get("/reports/recent/data")
def recent_reports_data(
    hours: int = Query(default=24, ge=1, le=720),
    limit: int = Query(default=200, ge=1, le=1000),
    service: TaskService = Depends(get_task_service),
) -> Dict[str, Any]:
    if service.settings.review_v2_enabled:
        return ReviewV2Views(service.review_v2_db).recent_report(hours=hours, limit=limit)
    return service.get_recent_task_report(hours=hours, limit=limit)


@router.get("/reports/recent.html")
def recent_reports_page(
    hours: int = Query(default=24, ge=1, le=720),
    limit: int = Query(default=200, ge=1, le=1000),
    service: TaskService = Depends(get_task_service),
) -> HTMLResponse:
    data = (
        ReviewV2Views(service.review_v2_db).recent_report(hours=hours, limit=limit)
        if service.settings.review_v2_enabled
        else service.get_recent_task_report(hours=hours, limit=limit)
    )
    return HTMLResponse(
        content=_render_recent_reports_page(
            data,
            {
                "hours": data["hours"],
                "limit": data["limit"],
            },
        )
    )


@router.get("/admin")
def admin_dashboard() -> HTMLResponse:
    html = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>cr_agent 后台</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <style>
    :root { --bg:#f4f7fb; --panel:#ffffff; --line:#d8e0eb; --text:#122033; --muted:#5f6f82; --accent:#0b63ce; --danger:#b42318; }
    body { margin:0; font-family:"PingFang SC","Microsoft YaHei",sans-serif; background:linear-gradient(180deg,#eef4fb 0%,#f8fafc 100%); color:var(--text); }
    .wrap { max-width: 1280px; margin: 0 auto; padding: 32px 24px 48px; }
    h1 { margin: 0 0 20px; font-size: 30px; }
    .toolbar, .chart-grid, .panel { background:var(--panel); border:1px solid var(--line); border-radius:18px; box-shadow:0 10px 30px rgba(18,32,51,.06); }
    .toolbar { padding:18px; display:flex; flex-wrap:wrap; gap:12px; align-items:end; margin-bottom:20px; }
    .field { display:flex; flex-direction:column; gap:6px; min-width:220px; }
    label { font-size:12px; color:var(--muted); }
    input, button { border:1px solid var(--line); border-radius:12px; padding:10px 12px; font:inherit; }
    button { background:var(--accent); color:#fff; border:none; cursor:pointer; }
    button.secondary { background:#fff; color:var(--text); border:1px solid var(--line); }
    button.danger { background:var(--danger); }
    .chart-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:0; overflow:hidden; margin-bottom:20px; }
    .chart-card { padding:18px; border-right:1px solid var(--line); }
    .chart-card:last-child { border-right:none; }
    .chart-wrap { position:relative; width:100%; aspect-ratio: 5 / 3; max-height: 320px; }
    .panel { padding:18px; }
    table { width:100%; border-collapse:collapse; }
    th, td { text-align:left; padding:10px 8px; border-top:1px solid #edf2f7; font-size:14px; vertical-align:top; }
    th { color:var(--muted); font-weight:600; border-top:none; }
    .row-actions { display:flex; gap:10px; align-items:center; margin-bottom:12px; }
    .pager { display:flex; gap:10px; align-items:center; justify-content:flex-end; margin-top:12px; }
    .muted { color:var(--muted); }
    code { background:#eef4ff; border-radius:8px; padding:2px 6px; }
    @media (max-width: 900px) { .chart-grid { grid-template-columns:1fr; } .chart-card { border-right:none; border-bottom:1px solid var(--line);} .chart-card:last-child{border-bottom:none;} }
  </style>
</head>
<body>
  <div class="wrap">
    <h1>cr_agent 后台管理</h1>
    <div class="toolbar">
      <div class="field">
        <label>开始时间</label>
        <input id="startAt" type="datetime-local">
      </div>
      <div class="field">
        <label>结束时间</label>
        <input id="endAt" type="datetime-local">
      </div>
      <div class="field" style="min-width:120px">
        <label>聚合分钟</label>
        <input id="bucketMinutes" type="number" min="1" value="60">
      </div>
      <button id="refreshBtn">刷新</button>
      <button id="deleteBtn" class="danger">删除该时间范围 running 任务</button>
      <span id="message" class="muted"></span>
    </div>

    <div class="panel" style="margin-bottom:20px;">
      <div class="row-actions">
        <h3 style="margin:0">误判范式同步</h3>
        <button id="syncBtn" class="secondary">手动触发范式同步</button>
        <span id="syncMessage" class="muted"></span>
      </div>
      <div class="muted">daemon 默认每天 23:38 执行一次；也可在这里手动触发。</div>
      <div id="syncState" class="muted" style="margin-top:8px;"></div>
    </div>

    <div class="chart-grid">
      <div class="chart-card">
        <h3>任务数量 - 时间</h3>
        <div class="chart-wrap"><canvas id="countChart"></canvas></div>
      </div>
      <div class="chart-card">
        <h3>任务时长 P99 - 时间</h3>
        <div class="chart-wrap"><canvas id="p99Chart"></canvas></div>
      </div>
    </div>

    <div class="panel" style="margin-bottom:20px;">
      <div class="row-actions">
        <h3 style="margin:0">模型成本报表</h3>
        <span id="costMeta" class="muted"></span>
      </div>
      <div style="display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; margin-bottom:16px;">
        <div style="border:1px solid var(--line); border-radius:14px; padding:14px;">
          <div class="muted">调用总次数</div>
          <div id="costTotalCalls" style="font-size:24px; font-weight:700;">0</div>
        </div>
        <div style="border:1px solid var(--line); border-radius:14px; padding:14px;">
          <div class="muted">Token 总量</div>
          <div id="costTotalTokens" style="font-size:24px; font-weight:700;">0</div>
        </div>
        <div style="border:1px solid var(--line); border-radius:14px; padding:14px;">
          <div class="muted">平均每次 Token</div>
          <div id="costAvgTokens" style="font-size:24px; font-weight:700;">0</div>
        </div>
        <div style="border:1px solid var(--line); border-radius:14px; padding:14px;">
          <div class="muted">输入/输出/思考 Token</div>
          <div id="costInOutThinkingTokens" style="font-size:18px; font-weight:700;">0 / 0 / 0</div>
        </div>
      </div>
      <div class="chart-grid" style="margin-bottom:0;">
        <div class="chart-card">
          <h3>Token 总量 - 时间</h3>
          <div class="chart-wrap"><canvas id="tokenChart"></canvas></div>
        </div>
        <div class="chart-card">
          <h3>各业务平均 Token</h3>
          <div class="chart-wrap"><canvas id="categoryTokenChart"></canvas></div>
        </div>
      </div>
      <div class="chart-grid" style="margin-top:20px; margin-bottom:0;">
        <div class="chart-card">
          <h3>Input/Output/Thinking Token 总量 - 时间</h3>
          <div class="chart-wrap"><canvas id="tokenBreakdownChart"></canvas></div>
        </div>
        <div class="chart-card">
          <h3>各业务平均 Input/Output/Thinking Token</h3>
          <div class="chart-wrap"><canvas id="categoryBreakdownChart"></canvas></div>
        </div>
      </div>
    </div>

    <div class="panel">
      <div class="row-actions">
        <h3 style="margin:0">运行中任务</h3>
        <span id="runningCount" class="muted"></span>
      </div>
      <table>
        <thead>
          <tr>
            <th>任务ID</th>
            <th>应用</th>
            <th>分支</th>
            <th>开始时间</th>
            <th>已运行秒数</th>
            <th>状态页</th>
          </tr>
        </thead>
        <tbody id="runningBody"></tbody>
      </table>
    </div>

    <div class="panel" style="margin-top:20px;">
      <div class="row-actions">
        <h3 style="margin:0">检查任务列表</h3>
        <span id="taskListMeta" class="muted"></span>
      </div>
      <table>
        <thead>
          <tr>
            <th>应用</th>
            <th>分支</th>
            <th>创建时间</th>
            <th>当前状态</th>
            <th>报告链接</th>
          </tr>
        </thead>
        <tbody id="taskListBody"></tbody>
      </table>
      <div class="pager">
        <button id="prevPageBtn" class="secondary">上一页</button>
        <span id="taskListPage" class="muted"></span>
        <button id="nextPageBtn" class="secondary">下一页</button>
      </div>
    </div>

    <div class="panel" style="margin-top:20px;">
      <div class="row-actions">
        <h3 style="margin:0">修复会话列表</h3>
        <span id="fixSessionMeta" class="muted"></span>
      </div>
      <table>
        <thead>
          <tr>
            <th>应用</th>
            <th>分支</th>
            <th>创建时间</th>
            <th>当前阶段</th>
            <th>处理状态</th>
            <th>MR</th>
            <th>报告</th>
          </tr>
        </thead>
        <tbody id="fixSessionBody"></tbody>
      </table>
      <div class="pager">
        <button id="prevFixSessionPageBtn" class="secondary">上一页</button>
        <span id="fixSessionPage" class="muted"></span>
        <button id="nextFixSessionPageBtn" class="secondary">下一页</button>
      </div>
    </div>
  </div>
  <script>
    let taskListPage = 1;
    const taskListPageSize = 20;
    let fixSessionPage = 1;
    const fixSessionPageSize = 20;
    const tokenChart = new Chart(document.getElementById('tokenChart'), {
      type: 'line',
      data: { labels: [], datasets: [{ label: 'Token 总量', data: [], borderColor: '#059669', backgroundColor: 'rgba(5,150,105,.12)', fill: true, tension: .25 }] },
      options: { responsive: true, maintainAspectRatio: false }
    });
    const categoryTokenChart = new Chart(document.getElementById('categoryTokenChart'), {
      type: 'bar',
      data: { labels: [], datasets: [{ label: '平均 Token', data: [], backgroundColor: 'rgba(99,102,241,.65)', borderColor: '#4f46e5', borderWidth: 1 }] },
      options: { responsive: true, maintainAspectRatio: false }
    });
    const tokenBreakdownChart = new Chart(document.getElementById('tokenBreakdownChart'), {
      type: 'line',
      data: {
        labels: [],
        datasets: [
          { label: 'Input', data: [], borderColor: '#0b63ce', backgroundColor: 'rgba(11,99,206,.10)', fill: false, tension: .25 },
          { label: 'Output', data: [], borderColor: '#059669', backgroundColor: 'rgba(5,150,105,.10)', fill: false, tension: .25 },
          { label: 'Thinking', data: [], borderColor: '#d97706', backgroundColor: 'rgba(217,119,6,.10)', fill: false, tension: .25 },
        ],
      },
      options: { responsive: true, maintainAspectRatio: false }
    });
    const categoryBreakdownChart = new Chart(document.getElementById('categoryBreakdownChart'), {
      type: 'bar',
      data: {
        labels: [],
        datasets: [
          { label: '平均 Input', data: [], backgroundColor: 'rgba(11,99,206,.70)' },
          { label: '平均 Output', data: [], backgroundColor: 'rgba(5,150,105,.70)' },
          { label: '平均 Thinking', data: [], backgroundColor: 'rgba(217,119,6,.70)' },
        ],
      },
      options: { responsive: true, maintainAspectRatio: false }
    });
    const countChart = new Chart(document.getElementById('countChart'), {
      type: 'line',
      data: { labels: [], datasets: [{ label: '任务数', data: [], borderColor: '#0b63ce', backgroundColor: 'rgba(11,99,206,.12)', fill: true, tension: .25 }] },
      options: { responsive: true, maintainAspectRatio: false }
    });
    const p99Chart = new Chart(document.getElementById('p99Chart'), {
      type: 'line',
      data: { labels: [], datasets: [{ label: 'P99 秒', data: [], borderColor: '#d97706', backgroundColor: 'rgba(217,119,6,.12)', fill: true, tension: .25 }] },
      options: { responsive: true, maintainAspectRatio: false }
    });

    function toShanghaiLabel(isoText) {
      if (!isoText) return '';
      return new Intl.DateTimeFormat('zh-CN', {
        timeZone: 'Asia/Shanghai',
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
        hour12: false,
      }).format(new Date(isoText));
    }

    function queryString() {
      const params = new URLSearchParams();
      const startAt = document.getElementById('startAt').value;
      const endAt = document.getElementById('endAt').value;
      const bucketMinutes = document.getElementById('bucketMinutes').value || '60';
      if (startAt) params.set('start_at', new Date(startAt).toISOString());
      if (endAt) params.set('end_at', new Date(endAt).toISOString());
      params.set('bucket_minutes', bucketMinutes);
      return params.toString();
    }

    async function refreshDashboard() {
      const qs = queryString();
      const metrics = await fetch(`/api/v1/admin/metrics?${qs}`, { cache: 'no-store' }).then(r => r.json());
      const running = await fetch('/api/v1/admin/running', { cache: 'no-store' }).then(r => r.json());
      const syncState = await fetch('/api/v1/admin/false-positive-sync', { cache: 'no-store' }).then(r => r.json());
      const costs = await fetch(`/api/v1/admin/costs?${qs}`, { cache: 'no-store' }).then(r => r.json());
      const taskList = await fetch(`/api/v1/admin/tasks?${qs}&page=${taskListPage}&page_size=${taskListPageSize}`, { cache: 'no-store' }).then(r => r.json());
      const fixSessions = await fetch(`/api/v1/admin/fix-sessions?${qs}&page=${fixSessionPage}&page_size=${fixSessionPageSize}`, { cache: 'no-store' }).then(r => r.json());

      countChart.data.labels = metrics.task_counts.map(item => toShanghaiLabel(item.time));
      countChart.data.datasets[0].data = metrics.task_counts.map(item => item.count);
      countChart.update();

      p99Chart.data.labels = metrics.duration_p99.map(item => toShanghaiLabel(item.time));
      p99Chart.data.datasets[0].data = metrics.duration_p99.map(item => item.p99_seconds);
      p99Chart.update();

      document.getElementById('runningCount').textContent = `共 ${running.length} 个`;
      const body = document.getElementById('runningBody');
      body.innerHTML = running.map(item => `
        <tr>
          <td><code>${item.task_id}</code></td>
          <td>${item.app_name}</td>
          <td>${item.branch}</td>
          <td>${item.started_at ? toShanghaiLabel(item.started_at) : '-'}</td>
          <td>${item.elapsed_seconds}</td>
          <td><a href="/task-status/${item.task_id}" target="_blank" rel="noopener">打开</a></td>
        </tr>
      `).join('') || '<tr><td colspan="6" class="muted">暂无运行中任务</td></tr>';

      document.getElementById('syncState').textContent =
        `状态: ${syncState.running ? '执行中' : '空闲'}；上次开始: ${syncState.last_started_at ? toShanghaiLabel(syncState.last_started_at) : '-'}；上次结束: ${syncState.last_finished_at ? toShanghaiLabel(syncState.last_finished_at) : '-'}；上次退出码: ${syncState.last_exit_code ?? '-'}`;
      document.getElementById('syncMessage').textContent = syncState.last_message || '';

      document.getElementById('costMeta').textContent = `共 ${costs.summary.total_calls} 次调用`;
      document.getElementById('costTotalCalls').textContent = costs.summary.total_calls;
      document.getElementById('costTotalTokens').textContent = costs.summary.total_tokens;
      document.getElementById('costAvgTokens').textContent = costs.summary.avg_tokens_per_call;
      document.getElementById('costInOutThinkingTokens').textContent = `${costs.summary.total_prompt_tokens} / ${costs.summary.total_completion_tokens} / ${costs.summary.total_thinking_tokens}`;
      tokenChart.data.labels = costs.tokens_over_time.map(item => toShanghaiLabel(item.time));
      tokenChart.data.datasets[0].data = costs.tokens_over_time.map(item => item.tokens);
      tokenChart.update();
      categoryTokenChart.data.labels = costs.avg_tokens_by_category.map(item => item.category);
      categoryTokenChart.data.datasets[0].data = costs.avg_tokens_by_category.map(item => item.avg_tokens);
      categoryTokenChart.update();
      tokenBreakdownChart.data.labels = costs.tokens_over_time.map(item => toShanghaiLabel(item.time));
      tokenBreakdownChart.data.datasets[0].data = costs.tokens_over_time.map(item => item.prompt_tokens);
      tokenBreakdownChart.data.datasets[1].data = costs.tokens_over_time.map(item => item.completion_tokens);
      tokenBreakdownChart.data.datasets[2].data = costs.tokens_over_time.map(item => item.thinking_tokens);
      tokenBreakdownChart.update();
      categoryBreakdownChart.data.labels = costs.avg_tokens_by_category.map(item => item.category);
      categoryBreakdownChart.data.datasets[0].data = costs.avg_tokens_by_category.map(item => item.avg_prompt_tokens);
      categoryBreakdownChart.data.datasets[1].data = costs.avg_tokens_by_category.map(item => item.avg_completion_tokens);
      categoryBreakdownChart.data.datasets[2].data = costs.avg_tokens_by_category.map(item => item.avg_thinking_tokens);
      categoryBreakdownChart.update();

      document.getElementById('taskListMeta').textContent = `共 ${taskList.total} 个`;
      document.getElementById('taskListBody').innerHTML = taskList.items.map(item => `
        <tr>
          <td>${item.app_name}</td>
          <td>${item.branch}</td>
          <td>${item.created_at ? toShanghaiLabel(item.created_at) : '-'}</td>
          <td>${item.status || '-'}</td>
          <td>${item.report_url ? `<a href="${item.report_url}" target="_blank" rel="noopener">打开</a>` : '-'}</td>
        </tr>
      `).join('') || '<tr><td colspan="5" class="muted">暂无任务</td></tr>';
      const totalPages = Math.max(1, Math.ceil(taskList.total / taskList.page_size));
      document.getElementById('taskListPage').textContent = `第 ${taskList.page} / ${totalPages} 页`;
      document.getElementById('prevPageBtn').disabled = taskList.page <= 1;
      document.getElementById('nextPageBtn').disabled = taskList.page >= totalPages;

      document.getElementById('fixSessionMeta').textContent = `共 ${fixSessions.total} 个`;
      document.getElementById('fixSessionBody').innerHTML = fixSessions.items.map(item => `
        <tr>
          <td>${item.app_name}</td>
          <td>${item.branch}</td>
          <td>${item.created_at ? toShanghaiLabel(item.created_at) : '-'}</td>
          <td>${item.stage || '-'}</td>
          <td>${item.processing ? '处理中' : '空闲'}</td>
          <td>${item.merge_request_url ? `<a href="${item.merge_request_url}" target="_blank" rel="noopener">打开</a>` : '-'}</td>
          <td>${item.report_url ? `<a href="${item.report_url}" target="_blank" rel="noopener">报告</a>` : '-'}</td>
        </tr>
      `).join('') || '<tr><td colspan="7" class="muted">暂无修复会话</td></tr>';
      const fixSessionTotalPages = Math.max(1, Math.ceil(fixSessions.total / fixSessions.page_size));
      document.getElementById('fixSessionPage').textContent = `第 ${fixSessions.page} / ${fixSessionTotalPages} 页`;
      document.getElementById('prevFixSessionPageBtn').disabled = fixSessions.page <= 1;
      document.getElementById('nextFixSessionPageBtn').disabled = fixSessions.page >= fixSessionTotalPages;
    }

    async function deleteRunning() {
      const qs = queryString();
      const resp = await fetch(`/api/v1/admin/running?${qs}`, { method: 'DELETE' }).then(r => r.json());
      document.getElementById('message').textContent = `已删除 ${resp.deleted_count} 个 running 任务`;
      refreshDashboard();
    }

    async function triggerFalsePositiveSync() {
      const resp = await fetch('/api/v1/admin/false-positive-sync', { method: 'POST' }).then(r => r.json());
      document.getElementById('syncMessage').textContent = resp.message || '';
      refreshDashboard();
    }

    async function prevTaskPage() {
      if (taskListPage <= 1) return;
      taskListPage -= 1;
      refreshDashboard();
    }

    async function nextTaskPage() {
      taskListPage += 1;
      refreshDashboard();
    }

    async function prevFixSessionPage() {
      if (fixSessionPage <= 1) return;
      fixSessionPage -= 1;
      refreshDashboard();
    }

    async function nextFixSessionPage() {
      fixSessionPage += 1;
      refreshDashboard();
    }

    document.getElementById('refreshBtn').addEventListener('click', refreshDashboard);
    document.getElementById('deleteBtn').addEventListener('click', deleteRunning);
    document.getElementById('syncBtn').addEventListener('click', triggerFalsePositiveSync);
    document.getElementById('prevPageBtn').addEventListener('click', prevTaskPage);
    document.getElementById('nextPageBtn').addEventListener('click', nextTaskPage);
    document.getElementById('prevFixSessionPageBtn').addEventListener('click', prevFixSessionPage);
    document.getElementById('nextFixSessionPageBtn').addEventListener('click', nextFixSessionPage);
    refreshDashboard();
  </script>
</body>
</html>"""
    return HTMLResponse(content=html)


@router.post("/api/v1/tasks/trigger", response_model=TriggerResponse)
def trigger_task(
    http_request: Request,
    request: TriggerRequest,
    _auth: None = Depends(_require_trigger_auth),
    service: TaskService = Depends(get_task_service),
) -> TriggerResponse:
    record = service.submit(request)
    return TriggerResponse(
        task_id=record.task_id,
        status=record.status,
        task_url=_task_status_url(http_request, record.task_id),
        report_url=record.report_url,
    )


@router.post("/api/v1/hooks/trigger", response_model=TriggerResponse)
def trigger_from_hook(
    http_request: Request,
    request: TriggerRequest,
    _auth: None = Depends(_require_trigger_auth),
    service: TaskService = Depends(get_task_service),
) -> TriggerResponse:
    record = service.submit(request)
    return TriggerResponse(
        task_id=record.task_id,
        status=record.status,
        task_url=_task_status_url(http_request, record.task_id),
        report_url=record.report_url,
    )


@router.post("/api/v1/ci/trigger")
def trigger_from_ci(
    request: Request,
    payload: Dict[str, Any],
    _auth: None = Depends(_require_trigger_auth),
    service: TaskService = Depends(get_task_service),
) -> JSONResponse:
    try:
        trigger_request = TriggerRequest.from_ci_payload(payload)
        record = service.submit(trigger_request)
        task_url = _task_status_url(request, record.task_id)
        return _ci_result(
            status=0,
            message="处理中",
            data={
                "taskId": record.task_id,
                "task_id": record.task_id,
                "status": record.status.value,
                "url": _report_or_task_url(request, record),
                "reportUrl": _report_or_task_url(request, record),
                "taskUrl": task_url,
            },
        )
    except Exception as exc:
        return _ci_result(
            status=-1,
            message=str(exc),
            data={},
        )


@router.get("/api/v1/tasks/{task_id}", response_model=TaskRecord)
def get_task(task_id: str, service: TaskService = Depends(get_task_service)) -> TaskRecord:
    record = service.get(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail="task not found")
    return record


@router.get("/api/v1/tasks", response_model=List[TaskRecord])
def list_tasks(service: TaskService = Depends(get_task_service)) -> List[TaskRecord]:
    return service.list_all()


@router.get("/reports/{task_id}/detail")
def report_detail(task_id: str, service: TaskService = Depends(get_task_service)) -> Dict[str, Any]:
    if service.settings.review_v2_enabled:
        data = ReviewV2Views(service.review_v2_db).report_detail(task_id)
        if not data:
            raise HTTPException(status_code=404, detail="task not found")
        return data
    return service.get_report_detail(task_id).model_dump(mode="json")


@router.post("/reports/{task_id}/findings/{finding_index}/feedback")
def submit_report_feedback(
    task_id: str,
    finding_index: int,
    payload: Dict[str, Any],
    service: TaskService = Depends(get_task_service),
) -> Dict[str, Any]:
    if service.settings.review_v2_enabled:
        raise HTTPException(status_code=404, detail="index-based feedback is not available in v2")
    message = (payload.get("message") or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is required")
    thread = service.submit_finding_feedback(task_id, finding_index, message)
    return {
        "thread": thread.model_dump(mode="json"),
        "message": "已提交大模型，等待回复",
    }


@router.post("/reports/{task_id}/findings/by-id/{finding_id}/feedback")
def submit_report_feedback_v2(
    task_id: str,
    finding_id: str,
    payload: Dict[str, Any],
    service: TaskService = Depends(get_task_service),
) -> Dict[str, Any]:
    if not service.settings.review_v2_enabled:
        raise HTTPException(status_code=404, detail="v2 feedback is not enabled")
    message = (payload.get("message") or payload.get("rationale") or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is required")
    processor = FeedbackProcessor(service.settings, service.review_v2_db)
    if payload.get("disposition") == "human_non_fix":
        try:
            processor.mark_human_non_fix(
                task_id=task_id,
                finding_id=finding_id,
                actor=str(payload.get("actor") or "human"),
                rationale=message,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"message": "已标记为人工非修复解决", "finding_id": finding_id}
    try:
        session_id = processor.submit_finding_feedback_background(task_id=task_id, finding_id=finding_id, message=message)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"message": "已提交反馈复审，正在后台执行", "finding_id": finding_id, "feedback_session_id": session_id}


@router.post("/reports/{task_id}/general-feedback")
def submit_general_report_feedback(
    task_id: str,
    payload: Dict[str, Any],
    service: TaskService = Depends(get_task_service),
) -> Dict[str, Any]:
    message = (payload.get("message") or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is required")
    item = service.submit_general_feedback(task_id, message)
    return {
        "feedback": item.model_dump(mode="json"),
        "message": "已提交漏判反馈，等待大模型复核",
    }


@router.post("/reports/{task_id}/fix-sessions")
def create_fix_session(
    task_id: str,
    payload: Dict[str, Any],
    service: TaskService = Depends(get_task_service),
) -> Dict[str, Any]:
    if service.settings.review_v2_enabled:
        raise HTTPException(status_code=404, detail="fix sessions are not available in v2")
    selected = payload.get("selected_finding_indexes") or []
    if not isinstance(selected, list):
        raise HTTPException(status_code=400, detail="selected_finding_indexes must be a list")
    session = service.create_fix_session(
        task_id,
        [int(item) for item in selected],
        (payload.get("message") or "").strip(),
    )
    return {
        "session": session.model_dump(mode="json"),
        "message": "已提交一键修复请求，等待大模型回复",
    }


@router.post("/reports/{task_id}/fix-sessions/{session_id}/messages")
def submit_fix_session_message(
    task_id: str,
    session_id: str,
    payload: Dict[str, Any],
    service: TaskService = Depends(get_task_service),
) -> Dict[str, Any]:
    message = (payload.get("message") or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is required")
    session = service.submit_fix_session_message(task_id, session_id, message)
    return {
        "session": session.model_dump(mode="json"),
        "message": "已提交大模型，等待回复",
    }


def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@router.get("/api/v1/admin/metrics")
def admin_metrics(
    start_at: Optional[str] = Query(default=None),
    end_at: Optional[str] = Query(default=None),
    bucket_minutes: int = Query(default=60, ge=1, le=1440),
    service: TaskService = Depends(get_task_service),
) -> Dict[str, Any]:
    return service.get_dashboard_metrics(
        start_at=_parse_datetime(start_at),
        end_at=_parse_datetime(end_at),
        bucket_minutes=bucket_minutes,
    )


@router.get("/api/v1/admin/costs")
def admin_costs(
    start_at: Optional[str] = Query(default=None),
    end_at: Optional[str] = Query(default=None),
    bucket_minutes: int = Query(default=60, ge=1, le=1440),
    service: TaskService = Depends(get_task_service),
) -> Dict[str, Any]:
    return service.get_cost_report(
        start_at=_parse_datetime(start_at),
        end_at=_parse_datetime(end_at),
        bucket_minutes=bucket_minutes,
    )


@router.get("/api/v1/admin/tasks")
def admin_task_list(
    start_at: Optional[str] = Query(default=None),
    end_at: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    service: TaskService = Depends(get_task_service),
) -> Dict[str, Any]:
    return service.list_tasks_page(
        start_at=_parse_datetime(start_at),
        end_at=_parse_datetime(end_at),
        page=page,
        page_size=page_size,
    )


@router.get("/api/v1/admin/fix-sessions")
def admin_fix_session_list(
    start_at: Optional[str] = Query(default=None),
    end_at: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    service: TaskService = Depends(get_task_service),
) -> Dict[str, Any]:
    return service.list_fix_sessions_page(
        start_at=_parse_datetime(start_at),
        end_at=_parse_datetime(end_at),
        page=page,
        page_size=page_size,
    )


@router.get("/api/v1/admin/running")
def admin_running_tasks(service: TaskService = Depends(get_task_service)) -> List[Dict[str, Any]]:
    now = datetime.now().astimezone()
    items = []
    for record in service.list_running_tasks():
        started_at = record.started_at or record.created_at
        items.append(
            {
                "task_id": record.task_id,
                "app_name": record.request.app_name,
                "branch": record.request.branch,
                "started_at": started_at.isoformat(),
                "elapsed_seconds": round((now - started_at.astimezone()).total_seconds(), 1),
            }
        )
    return items


@router.delete("/api/v1/admin/running")
def admin_delete_running_tasks(
    start_at: Optional[str] = Query(default=None),
    end_at: Optional[str] = Query(default=None),
    service: TaskService = Depends(get_task_service),
) -> Dict[str, Any]:
    return service.delete_running_tasks(
        start_at=_parse_datetime(start_at),
        end_at=_parse_datetime(end_at),
    )


def _review_v2_task_control_response(task_id: str, action: str, service: TaskService) -> Dict[str, Any]:
    data = ReviewV2Views(service.review_v2_db).task_status(task_id)
    if not data:
        raise HTTPException(status_code=404, detail="task not found")
    return {"task_id": task_id, "action": action, "status": data}


def _ensure_review_v2_task(task_id: str, service: TaskService) -> None:
    if service.review_v2_db.get_task(task_id) is None:
        raise HTTPException(status_code=404, detail="task not found")


@router.post("/api/v1/admin/tasks/{task_id}/stop")
def admin_stop_review_v2_task(
    task_id: str,
    reason: Optional[str] = Query(default=None),
    service: TaskService = Depends(get_task_service),
) -> Dict[str, Any]:
    if not service.settings.review_v2_enabled:
        raise HTTPException(status_code=404, detail="task control is only available for review v2")
    _ensure_review_v2_task(task_id, service)
    service.review_v2_db.request_task_stop(task_id, reason=reason)
    return _review_v2_task_control_response(task_id, "stop", service)


@router.post("/api/v1/admin/tasks/{task_id}/cancel")
def admin_cancel_review_v2_task(
    task_id: str,
    reason: Optional[str] = Query(default=None),
    service: TaskService = Depends(get_task_service),
) -> Dict[str, Any]:
    if not service.settings.review_v2_enabled:
        raise HTTPException(status_code=404, detail="task control is only available for review v2")
    _ensure_review_v2_task(task_id, service)
    service.review_v2_db.cancel_task(task_id, reason=reason)
    return _review_v2_task_control_response(task_id, "cancel", service)


@router.post("/api/v1/admin/tasks/{task_id}/requeue")
def admin_requeue_review_v2_task(
    task_id: str,
    reason: Optional[str] = Query(default=None),
    service: TaskService = Depends(get_task_service),
) -> Dict[str, Any]:
    if not service.settings.review_v2_enabled:
        raise HTTPException(status_code=404, detail="task control is only available for review v2")
    _ensure_review_v2_task(task_id, service)
    service.review_v2_db.requeue_task(task_id, reason=reason)
    return _review_v2_task_control_response(task_id, "requeue", service)


@router.get("/api/v1/admin/false-positive-sync")
def admin_false_positive_sync_state() -> Dict[str, Any]:
    return _get_false_positive_sync_state()


@router.post("/api/v1/admin/false-positive-sync")
def admin_trigger_false_positive_sync(service: TaskService = Depends(get_task_service)) -> Dict[str, Any]:
    return _trigger_false_positive_sync(service)

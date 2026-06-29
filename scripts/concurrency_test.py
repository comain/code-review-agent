#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

TERMINAL_STATUSES = {"success", "failed"}


@dataclass
class SubmitResult:
    index: int
    request: Dict[str, Any]
    task_id: Optional[str]
    status: str
    elapsed_ms: int
    error: Optional[str] = None


@dataclass
class OpencodeResult:
    index: int
    returncode: int
    elapsed_ms: int
    stdout_chars: int
    stderr_chars: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    error: Optional[str] = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="cr_agent / opencode 并发压测脚本")
    parser.add_argument(
        "--mode",
        choices=["service", "opencode"],
        default="service",
        help="service=通过 cr_agent 触发真实任务；opencode=直接并发压 opencode 模型调用",
    )

    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="cr_agent 服务地址")
    parser.add_argument("--endpoint", default="/api/v1/tasks/trigger", help="触发接口路径")
    parser.add_argument("--total", type=int, default=8, help="总触发次数/总调用次数")
    parser.add_argument("--submit-concurrency", type=int, default=4, help="并发提交数/并发调用数")
    parser.add_argument("--poll-interval", type=float, default=5.0, help="状态轮询间隔秒")
    parser.add_argument("--timeout-seconds", type=int, default=3600, help="整体等待超时时间")
    parser.add_argument("--request-timeout", type=float, default=30.0, help="单次 HTTP 超时秒")
    parser.add_argument("--cases-file", help="JSON/JSONL 请求样例文件；用于压真实执行并发")

    parser.add_argument("--app-name")
    parser.add_argument("--repo-url")
    parser.add_argument("--branch")
    parser.add_argument("--commit-id")
    parser.add_argument("--callback-url")
    parser.add_argument("--callback-token")
    parser.add_argument("--operator", default="load-test")
    parser.add_argument("--trigger-source", default="manual-load-test")
    parser.add_argument("--metadata-json", default="{}", help="附加 metadata JSON 字符串")

    parser.add_argument("--cost-report", action="store_true", help="service 模式下额外打印 admin 成本报表增量")

    parser.add_argument("--opencode-bin", default="opencode", help="opencode 可执行文件")
    parser.add_argument("--opencode-model", default="llm-proxy/gpt-5.5", help="直接压测时使用的 provider/model")
    parser.add_argument("--opencode-repo-path", help="直接压测时的 --dir 路径")
    parser.add_argument("--opencode-prompt-file", help="直接压测时使用的 prompt 文件")
    parser.add_argument(
        "--opencode-prompt",
        default='请只输出 {"ok":true}',
        help="直接压测时使用的 prompt 内容；若同时传 --opencode-prompt-file，则以文件为准",
    )
    parser.add_argument("--opencode-log-level", default="INFO", help="直接压测时传给 opencode 的日志级别")
    return parser.parse_args()


def load_cases(args: argparse.Namespace) -> List[Dict[str, Any]]:
    if args.cases_file:
        content = Path(args.cases_file).read_text(encoding="utf-8").strip()
        if not content:
            raise ValueError("cases file is empty")
        if content.startswith("["):
            payload = json.loads(content)
            if not isinstance(payload, list):
                raise ValueError("cases file JSON must be a list")
            return [normalize_case(item) for item in payload]
        return [normalize_case(json.loads(line)) for line in content.splitlines() if line.strip()]

    required = {
        "app_name": args.app_name,
        "repo_url": args.repo_url,
        "branch": args.branch,
    }
    missing = [key for key, value in required.items() if not value]
    if missing:
        raise ValueError(f"missing required args for single-case mode: {', '.join(missing)}")

    metadata = json.loads(args.metadata_json or "{}")
    if not isinstance(metadata, dict):
        raise ValueError("metadata-json must decode to object")
    return [
        normalize_case(
            {
                "app_name": args.app_name,
                "repo_url": args.repo_url,
                "branch": args.branch,
                "commit_id": args.commit_id,
                "callback_url": args.callback_url,
                "callback_token": args.callback_token,
                "operator": args.operator,
                "trigger_source": args.trigger_source,
                "metadata": metadata,
            }
        )
    ]


def normalize_case(payload: Dict[str, Any]) -> Dict[str, Any]:
    case = dict(payload)
    case.setdefault("operator", "load-test")
    case.setdefault("trigger_source", "manual-load-test")
    case.setdefault("metadata", {})
    return case


def build_requests(cases: List[Dict[str, Any]], total: int) -> List[Dict[str, Any]]:
    return [dict(cases[index % len(cases)]) for index in range(total)]


def submit_once(
    client: Any,
    url: str,
    request_body: Dict[str, Any],
    index: int,
) -> SubmitResult:
    started = time.time()
    try:
        response = client.post(url, json=request_body)
        response.raise_for_status()
        data = response.json()
        task_id = safe_get(data, "data", "taskId") or data.get("task_id")
        status = safe_get(data, "data", "status") or data.get("status") or "accepted"
        return SubmitResult(
            index=index,
            request=request_body,
            task_id=task_id,
            status=str(status),
            elapsed_ms=int((time.time() - started) * 1000),
        )
    except Exception as exc:  # noqa: BLE001
        response_text = ""
        httpx_module = sys.modules.get("httpx")
        if httpx_module is not None and isinstance(exc, httpx_module.HTTPStatusError):
            response_text = exc.response.text[:1000]
        message = str(exc)
        if response_text:
            message = f"{message}; body={response_text}"
        return SubmitResult(
            index=index,
            request=request_body,
            task_id=None,
            status="submit_failed",
            elapsed_ms=int((time.time() - started) * 1000),
            error=message,
        )


def poll_tasks(
    client: Any,
    base_url: str,
    task_ids: List[str],
    timeout_seconds: int,
    poll_interval: float,
) -> Dict[str, Dict[str, Any]]:
    deadline = time.time() + timeout_seconds
    remaining = set(task_ids)
    results: Dict[str, Dict[str, Any]] = {}

    while remaining and time.time() < deadline:
        for task_id in list(remaining):
            response = client.get(f"{base_url}/api/v1/tasks/{task_id}")
            response.raise_for_status()
            data = response.json()
            status = data["status"]
            results[task_id] = data
            if status in TERMINAL_STATUSES:
                remaining.discard(task_id)
        if remaining:
            time.sleep(poll_interval)

    return results


def fetch_cost_report(client: httpx.Client, base_url: str, started_at: datetime) -> Dict[str, Any]:
    params = {"start_at": started_at.astimezone(timezone.utc).isoformat(), "bucket_minutes": 60}
    response = client.get(f"{base_url}/api/v1/admin/costs", params=params)
    response.raise_for_status()
    return response.json()


def print_submit_summary(submit_results: List[SubmitResult]) -> None:
    print("== 提交结果 ==")
    for item in submit_results:
        line = (
            f"[{item.index:02d}] task_id={item.task_id} status={item.status} "
            f"submit_ms={item.elapsed_ms} app={item.request.get('app_name')} branch={item.request.get('branch')}"
        )
        if item.error:
            line += f" error={item.error}"
        print(line)

    latencies = [item.elapsed_ms for item in submit_results]
    unique_task_ids = {item.task_id for item in submit_results if item.task_id}
    failed_submits = sum(1 for item in submit_results if item.status == "submit_failed")
    print()
    print(f"提交次数: {len(submit_results)}")
    print(f"唯一 task_id 数: {len(unique_task_ids)}")
    print(f"提交失败数: {failed_submits}")
    print(f"提交耗时 avg={statistics.mean(latencies):.1f}ms p95={percentile(latencies, 95):.1f}ms")
    if len(unique_task_ids) < len(submit_results):
        print("注意: 发现重复 task_id，说明幂等去重生效；相同 app/repo/branch/commit 的并发提交会折叠成同一任务。")


def print_final_summary(
    submit_results: List[SubmitResult],
    final_results: Dict[str, Dict[str, Any]],
    started_at: float,
) -> None:
    print()
    print("== 最终结果 ==")
    unique_task_ids = []
    seen = set()
    for item in submit_results:
        if not item.task_id:
            continue
        if item.task_id in seen:
            continue
        seen.add(item.task_id)
        unique_task_ids.append(item.task_id)

    status_counter = Counter()
    durations = []
    for task_id in unique_task_ids:
        data = final_results.get(task_id)
        if not data:
            status_counter["timeout"] += 1
            print(f"{task_id} status=timeout")
            continue

        status = data["status"]
        status_counter[status] += 1
        duration_seconds = calc_duration_seconds(data)
        if duration_seconds is not None:
            durations.append(duration_seconds)
        print(
            f"{task_id} status={status} score={safe_get(data, 'result', 'score')} "
            f"report={data.get('report_url')} error={data.get('error_message')}"
        )

    print()
    print(f"唯一任务数: {len(unique_task_ids)}")
    print(f"状态分布: {dict(status_counter)}")
    print(f"总墙钟耗时: {time.time() - started_at:.1f}s")
    if durations:
        print(
            f"单任务执行耗时 avg={statistics.mean(durations):.1f}s "
            f"p95={percentile(durations, 95):.1f}s max={max(durations):.1f}s"
        )


def calc_duration_seconds(task_data: Dict[str, Any]) -> Optional[float]:
    started_at = task_data.get("started_at")
    finished_at = task_data.get("finished_at")
    if not started_at or not finished_at:
        return None
    try:
        started_epoch = parse_iso_utc(started_at)
        finished_epoch = parse_iso_utc(finished_at)
    except ValueError:
        return None
    return max(finished_epoch - started_epoch, 0.0)


def parse_iso_utc(value: str) -> float:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value).timestamp()


def percentile(values: List[float], p: int) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    index = max(int(round((p / 100) * len(values) + 0.5)) - 1, 0)
    index = min(index, len(values) - 1)
    return float(values[index])


def safe_get(data: Dict[str, Any], *keys: str) -> Any:
    current: Any = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def build_opencode_prompt(args: argparse.Namespace) -> str:
    if args.opencode_prompt_file:
        return Path(args.opencode_prompt_file).read_text(encoding="utf-8")
    return args.opencode_prompt


def extract_usage_metrics(text: str) -> Dict[str, int]:
    prompt_tokens = 0
    completion_tokens = 0
    total_tokens = 0
    seen = False

    def normalize_int(value: Any) -> Optional[int]:
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
        return None

    def scan(obj: Any) -> None:
        nonlocal prompt_tokens, completion_tokens, total_tokens, seen
        if isinstance(obj, dict):
            lower = {str(key).lower(): value for key, value in obj.items()}
            input_value = normalize_int(
                lower.get("inputtokens")
                or lower.get("prompttokens")
                or lower.get("input_tokens")
                or lower.get("prompt_tokens")
            )
            output_value = normalize_int(
                lower.get("outputtokens")
                or lower.get("completiontokens")
                or lower.get("output_tokens")
                or lower.get("completion_tokens")
            )
            total_value = normalize_int(lower.get("totaltokens") or lower.get("total_tokens"))
            nested_tokens = lower.get("tokens")
            if isinstance(nested_tokens, dict):
                nested_lower = {str(key).lower(): value for key, value in nested_tokens.items()}
                input_value = input_value if input_value is not None else normalize_int(
                    nested_lower.get("input")
                    or nested_lower.get("prompt")
                    or nested_lower.get("inputtokens")
                    or nested_lower.get("prompttokens")
                )
                output_value = output_value if output_value is not None else normalize_int(
                    nested_lower.get("output")
                    or nested_lower.get("completion")
                    or nested_lower.get("outputtokens")
                    or nested_lower.get("completiontokens")
                )
                total_value = total_value if total_value is not None else normalize_int(
                    nested_lower.get("total") or nested_lower.get("totaltokens")
                )
            if input_value is not None or output_value is not None or total_value is not None:
                seen = True
                if input_value is not None:
                    prompt_tokens = max(prompt_tokens, input_value)
                if output_value is not None:
                    completion_tokens = max(completion_tokens, output_value)
                if total_value is not None:
                    total_tokens = max(total_tokens, total_value)
            for value in obj.values():
                scan(value)
        elif isinstance(obj, list):
            for item in obj:
                scan(item)

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            scan(json.loads(line))
        except json.JSONDecodeError:
            continue

    if total_tokens == 0 and (prompt_tokens or completion_tokens):
        total_tokens = prompt_tokens + completion_tokens
    if not seen:
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }


def run_opencode_once(
    opencode_bin: str,
    model: str,
    repo_path: str,
    prompt: str,
    log_level: str,
    timeout_seconds: int,
    index: int,
) -> OpencodeResult:
    started = time.time()
    cmd = [
        opencode_bin,
        "run",
        "--dir",
        repo_path,
        "--format",
        "json",
        "--print-logs",
        "--log-level",
        log_level,
        "-m",
        model,
    ]
    try:
        completed = subprocess.run(
            cmd,
            input=prompt,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
        usage = extract_usage_metrics(completed.stdout or "")
        return OpencodeResult(
            index=index,
            returncode=completed.returncode,
            elapsed_ms=int((time.time() - started) * 1000),
            stdout_chars=len(completed.stdout or ""),
            stderr_chars=len(completed.stderr or ""),
            prompt_tokens=usage["prompt_tokens"],
            completion_tokens=usage["completion_tokens"],
            total_tokens=usage["total_tokens"],
            error=None if completed.returncode == 0 else (completed.stderr or completed.stdout)[:1000],
        )
    except subprocess.TimeoutExpired as exc:
        return OpencodeResult(
            index=index,
            returncode=124,
            elapsed_ms=int((time.time() - started) * 1000),
            stdout_chars=len(exc.stdout or ""),
            stderr_chars=len(exc.stderr or ""),
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            error="timeout",
        )


def print_opencode_summary(results: List[OpencodeResult], started_at: float, model: str, repo_path: str) -> None:
    print("== 直接 opencode 压测结果 ==")
    for item in sorted(results, key=lambda x: x.index):
        line = (
            f"[{item.index:02d}] rc={item.returncode} elapsed_ms={item.elapsed_ms} "
            f"tokens={item.total_tokens} prompt={item.prompt_tokens} completion={item.completion_tokens} "
            f"stdout_chars={item.stdout_chars} stderr_chars={item.stderr_chars}"
        )
        if item.error:
            line += f" error={item.error}"
        print(line)

    elapsed_values = [item.elapsed_ms / 1000 for item in results]
    success_count = sum(1 for item in results if item.returncode == 0)
    total_tokens = sum(item.total_tokens for item in results)
    total_prompt = sum(item.prompt_tokens for item in results)
    total_completion = sum(item.completion_tokens for item in results)
    print()
    print(f"模型: {model}")
    print(f"目录: {repo_path}")
    print(f"总调用数: {len(results)}")
    print(f"成功数: {success_count}")
    print(f"失败数: {len(results) - success_count}")
    print(f"总墙钟耗时: {time.time() - started_at:.1f}s")
    print(
        f"单次调用耗时 avg={statistics.mean(elapsed_values):.1f}s "
        f"p95={percentile(elapsed_values, 95):.1f}s max={max(elapsed_values):.1f}s"
    )
    print(f"Token 总量: {total_tokens} (prompt={total_prompt}, completion={total_completion})")
    print(f"平均每次 Token: {round(total_tokens / len(results), 1) if results else 0}")


def run_service_mode(args: argparse.Namespace) -> int:
    import httpx

    try:
        cases = load_cases(args)
    except Exception as exc:  # noqa: BLE001
        print(f"参数错误: {exc}", file=sys.stderr)
        return 2

    requests = build_requests(cases, args.total)
    base_url = args.base_url.rstrip("/")
    trigger_url = f"{base_url}{args.endpoint}"
    started_at = time.time()
    report_started_at = datetime.now(timezone.utc)

    print(f"模式: service")
    print(f"服务地址: {base_url}")
    print(f"触发接口: {trigger_url}")
    print(f"总请求数: {len(requests)}")
    print(f"并发提交数: {args.submit_concurrency}")
    print(f"请求模板数: {len(cases)}")
    if len(cases) == 1 and args.total > 1:
        print("提示: 当前只有 1 个请求模板，相同请求可能被服务幂等去重，无法形成真实执行并发。")
    print()

    submit_results: List[SubmitResult] = []
    with httpx.Client(timeout=args.request_timeout) as client:
        with ThreadPoolExecutor(max_workers=args.submit_concurrency) as executor:
            future_map = {
                executor.submit(submit_once, client, trigger_url, request_body, index): index
                for index, request_body in enumerate(requests, start=1)
            }
            for future in as_completed(future_map):
                submit_results.append(future.result())

        submit_results.sort(key=lambda item: item.index)
        print_submit_summary(submit_results)

        task_ids = [item.task_id for item in submit_results if item.task_id]
        final_results = poll_tasks(
            client,
            base_url=base_url,
            task_ids=task_ids,
            timeout_seconds=args.timeout_seconds,
            poll_interval=args.poll_interval,
        )

        print_final_summary(submit_results, final_results, started_at)

        if args.cost_report:
            cost_report = fetch_cost_report(client, base_url, report_started_at)
            print()
            print("== 成本报表增量 ==")
            print(
                f"调用总次数: {cost_report['summary']['total_calls']} "
                f"Token 总量: {cost_report['summary']['total_tokens']} "
                f"平均每次 Token: {cost_report['summary']['avg_tokens_per_call']}"
            )
            for item in cost_report["avg_tokens_by_category"]:
                print(
                    f"{item['category']}: calls={item['calls']} "
                    f"avg_tokens={item['avg_tokens']} total_tokens={item['total_tokens']}"
                )
    return 0


def run_opencode_mode(args: argparse.Namespace) -> int:
    if not args.opencode_repo_path:
        print("参数错误: opencode 模式必须传 --opencode-repo-path", file=sys.stderr)
        return 2

    prompt = build_opencode_prompt(args)
    started_at = time.time()
    print(f"模式: opencode")
    print(f"模型: {args.opencode_model}")
    print(f"目录: {args.opencode_repo_path}")
    print(f"总调用数: {args.total}")
    print(f"并发调用数: {args.submit_concurrency}")
    print()

    results: List[OpencodeResult] = []
    with ThreadPoolExecutor(max_workers=args.submit_concurrency) as executor:
        future_map = {
            executor.submit(
                run_opencode_once,
                args.opencode_bin,
                args.opencode_model,
                args.opencode_repo_path,
                prompt,
                args.opencode_log_level,
                args.timeout_seconds,
                index,
            ): index
            for index in range(1, args.total + 1)
        }
        for future in as_completed(future_map):
            results.append(future.result())

    print_opencode_summary(results, started_at, args.opencode_model, args.opencode_repo_path)
    return 0


def main() -> int:
    args = parse_args()
    if args.mode == "opencode":
        return run_opencode_mode(args)
    return run_service_mode(args)


if __name__ == "__main__":
    raise SystemExit(main())

# CR Agent Architecture

## Overview

`cr_agent` is a single-process FastAPI service that accepts static analysis trigger requests, prepares a local git workspace, invokes `opencode`, normalizes the result, writes a report, and sends a callback.

It now also provides:

- an admin dashboard for operational metrics and running-task cleanup
- a task-status page for CI / users
- a report feedback workflow where users can challenge findings and the model re-reviews them asynchronously
- daily false-positive pattern extraction that can update the static-check skill and open an MR

## Components

```mermaid
flowchart LR
    A["Caller / CI / Manual Trigger"] --> B["FastAPI API<br/>routes.py"]
    B --> C["TaskService<br/>service.py"]
    C --> D["TaskStore<br/>runtime/tasks/*.json"]
    C --> E["In-Memory Queue<br/>queue.Queue"]
    C --> E2["Feedback Queue<br/>queue.Queue"]
    E --> F["Worker Threads<br/>ThreadPoolExecutor"]
    F --> G["GitClient<br/>clone/fetch/checkout/diff"]
    F --> H["OpenCode Execution<br/>prompt + opencode run"]
    H --> I["Prompt Assets<br/>src/cr_agent/review_v2/templates/*"]
    F --> J["ReportWriter<br/>runtime/reports/<task_id>"]
    F --> K["CallbackClient<br/>HTTP callback / CI ack"]
    E2 --> N["Feedback Worker Thread<br/>model re-review"]
    N --> H
    C --> O["FeedbackStore<br/>runtime/reports/<task_id>/feedback.json"]
    N --> P["False Positive Archive<br/>/opt/app/issues/*.jsonl"]
    C --> L["Recovery Thread<br/>requeue orphan tasks"]
    G --> M["Repo Cache<br/>runtime/repos/*"]
    B --> Q["Admin UI / Report UI / Task Status UI"]
```

## Runtime Flow

```mermaid
sequenceDiagram
    participant U as Caller
    participant API as FastAPI
    participant S as TaskService
    participant Q as Queue/Workers
    participant G as GitClient
    participant O as OpencodeRunner
    participant R as ReportWriter
    participant C as CallbackClient

    U->>API: POST /api/v1/tasks/trigger
    API->>S: submit(request)
    S->>S: inflight dedupe
    S->>Q: enqueue task_id
    API-->>U: task_id + queued/running

    Q->>S: _run_task(task_id)
    S->>G: prepare_repo()
    G->>G: clone/fetch/checkout/clean
    S->>G: collect_review_context()
    G-->>S: diff_range + changed files + commit log
    S->>O: analyze(task_id, repo, context)
    O->>O: write <task_id>.prompt.md
    O->>O: run opencode
    O->>O: parse output
    alt malformed output
        O->>O: write <task_id>.repair.N.prompt.md
        O->>O: run opencode repair
    end
    O-->>S: AnalysisResult
    S->>S: normalize findings to changed non-test files
    S->>R: write report files
    R-->>S: report_url + report_file
    S->>C: send callback
    C-->>S: callback history
    S->>S: persist terminal task state
```

## Report Feedback Flow

```mermaid
sequenceDiagram
    participant U as User
    participant API as FastAPI
    participant S as TaskService
    participant FQ as Feedback Queue
    participant G as GitClient
    participant O as OpencodeRunner
    participant FS as FeedbackStore
    participant IA as /opt/app/issues

    U->>API: POST /reports/{task_id}/findings/{index}/feedback
    API->>S: submit_finding_feedback(...)
    S->>FS: append user message + mark processing=true
    S->>FQ: enqueue(task_id, finding_index)
    API-->>U: 已提交大模型，等待回复

    loop background feedback worker
        FQ->>S: _process_feedback_job
        S->>G: prepare_repo() + read code context
        S->>O: review_finding_feedback(...)
        O-->>S: keep / downgrade / resolve_false_positive
        S->>FS: append model reply + update thread state
        alt false positive
            S->>IA: append false-positive pattern jsonl
        end
    end

    loop UI polling
        U->>API: GET /reports/{task_id}/detail
        API->>S: get_report_detail()
        S->>FS: merge feedback thread state into findings
        API-->>U: updated findings + score + summary
    end
```

## External Access Paths

- `POST /api/v1/ci/trigger`
  - CI trigger entrypoint
- `GET /task-status/{task_id}`
  - task lifecycle status page
- `GET /reports/{task_id}/index.html`
  - static report page
- `GET /reports/{task_id}/detail`
  - report detail JSON for UI polling
- `POST /reports/{task_id}/findings/{finding_index}/feedback`
  - asynchronous finding feedback submission
- `GET /admin`
  - admin dashboard
- `GET /api/v1/admin/metrics`
  - task count / p99 series
- `GET|DELETE /api/v1/admin/running`
  - running task list / bulk cleanup
- `GET /health`
- `GET /healthcheck.html`

## Persistence and Recovery

- Task metadata is persisted as JSON files under `runtime/tasks`.
- Reports are persisted under `runtime/reports/<task_id>`.
- Per-report feedback threads are persisted under `runtime/reports/<task_id>/feedback.json`.
- False-positive patterns are appended to `/opt/app/issues/YYYY-MM-DD.false-positive.jsonl`.
- A startup recovery pass requeues unfinished tasks.
- A background recovery thread scans `running` tasks and requeues or fails tasks that appear orphaned.
- Duplicate callbacks are suppressed by `callback_payload_digest`.

## Current Operational Limits

- Task queue and worker pool are in-memory within one service process.
- Feedback re-review queue is also in-memory within one service process.
- Horizontal scaling is not built in; multiple instances would need shared storage and coordination.
- `opencode` execution is the dominant throughput bottleneck, not the HTTP layer.
- Report feedback submission is asynchronous, but model re-review still consumes the same local `opencode` capacity.

## Daily False-Positive Sync

```mermaid
flowchart LR
    A["/opt/app/issues/YYYY-MM-DD.false-positive.jsonl"] --> B["scripts/sync_false_positive_patterns.py"]
    B --> C["Isolated git clone<br/>runtime/sync_worktrees/*"]
    C --> D["src/cr_agent/review_v2/templates/references/false_positive_patterns.md"]
    C --> E["src/cr_agent/review_v2/templates/references/review.md"]
    C --> F["git commit + push mr/* branch"]
    F --> G["GitLab MR to init<br/>(token if available)"]
```

Behavior:

- The script scans the current day's false-positive archive.
- It clones a fresh isolated working directory instead of editing the running repo.
- It updates the CR v2 packaged false-positive patterns reference and, if needed, the packaged guideline asset.
- It pushes a new `mr/...` branch and attempts to create an MR to `init`.
- If no GitLab token is available, it falls back to printing a manual MR link.

## Logging Model

Key lifecycle logs are emitted with `task=<task_id>` so operators can trace:

- submit / dedupe / enqueue
- worker start / finish
- repo prepare and review-context collection
- opencode analyze start / finish
- parse failure and repair attempts
- report generation
- callback attempts and dedupe
- orphan recovery / timeout

For report feedback, additional logs matter:

- feedback submission accepted
- feedback worker start / finish
- opencode feedback review parse failures
- false-positive archive append

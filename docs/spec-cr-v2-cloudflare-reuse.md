# Spec: CR V2 Cloudflare-Style Review Orchestration With comain/unit-test-agent Reuse

## Status

Draft for approval. This is a Phase 1 spec artifact only; implementation and detailed design should wait for explicit approval.

This is confirmed non-Ticket tool work. Use `docs/` artifacts rather than Ticket-keyed `doc/` artifacts.

## Confirmed Product/Architecture Decisions

- Use SQLite in this version for tasks, reviewer runs, finding records, finding events, progress, token usage, and dashboard queries.
- CI/CI/manual triggers only create queued tasks in SQLite. A separate CR daemon claims queued tasks from SQLite and runs the LangGraph workflow, following [comain/unit-test-agent](https://github.com/comain/unit-test-agent) task manager/daemon pattern.
- The daemon has a CLI entry point with terminal dashboard and operational parameters for worker identity, concurrency, polling interval, claim limit, once/run-forever mode, stale-task recovery, and callback retry processing.
- Copy/adapt [comain/unit-test-agent](https://github.com/comain/unit-test-agent) modules directly into `cr_agent`; do not create a shared package in this version.
- Use LangGraph in this version for the CR workflow.
- Reuse [comain/unit-test-agent](https://github.com/comain/unit-test-agent) OpenCode config generation and model-selection/routing logic.
- Reuse/adapt context preparation for LLM tasks: deterministic diff construction, CI/CI request context normalization, and prompt templates that reference current review skills/guidelines.
- Replace the current v1 CR execution path directly; do not maintain a compatibility window for index-based finding operations or JSON task storage.
- Reuse/adapt the [comain/unit-test-agent](https://github.com/comain/unit-test-agent) task dashboard and recent-job/progress patterns for CR tasks.
- Add deterministic workflow guards like [comain/unit-test-agent](https://github.com/comain/unit-test-agent): stage gates, required artifact checks, session ID checks, reviewer completion checks, stale-runner handling, and explicit failed/incomplete states.
- Keep raw audit artifacts private; public report artifacts must be sanitized and must not expose raw diffs, prompts, prompt inputs, or OpenCode JSONL logs.
- Do not allow zero-review success for production diffs. Ignored-file-only changes may be marked `skipped`, not clean reviewed.

## Background

`cr_agent` currently runs one large OpenCode review prompt, parses one final `AnalysisResult`, writes report artifacts, and supports follow-up operations through index-based feedback threads and fix sessions.

Recent production issues show that this shape is too opaque:

- a failed or partial review can look like a clean pass
- "0 files investigated" and empty findings are hard to distinguish from a real empty review
- session IDs and token usage are reconstructed after the fact instead of being first-class run data
- all findings are array-index addressed, which breaks once results are deduped, merged, reordered, or produced by multiple reviewers
- the single prompt makes latency and quality problems hard to attribute to a reviewer, model, tool call, or format repair step

The preferred v2 direction remains Cloudflare-style orchestration: a coordinator selects bounded specialist reviewer sessions, each reviewer has its own OpenCode session, and a judge/coordinator pass normalizes and dedupes findings. We should reuse code and patterns from the public [comain/unit-test-agent](https://github.com/comain/unit-test-agent) repo instead of rebuilding the workflow substrate.

## Goals

- Reuse existing [comain/unit-test-agent](https://github.com/comain/unit-test-agent) infrastructure where it is language-neutral or easy to adapt:
  - LangGraph workflow state and deterministic transitions
  - OpenCode process spawning, JSONL stream parsing, session ID capture, timeout/stall handling, and token aggregation
  - LLM session recovery patterns
  - task manager concepts: durable task status, events, heartbeats, session IDs, token/cost fields
  - recent jobs/progress UI patterns
- Preserve only the externally required trigger/report/callback contract:
  - existing trigger APIs should keep accepting CI/manual requests unless the design proves a breaking route change is necessary
  - new v2 report URLs and callback payloads should remain usable by CI
  - old static report files remain best-effort through `StaticFiles`; old JSON-backed dynamic report detail, feedback, and fix-session interactions are not fixed in this version
  - internal storage, task state, report detail backing data, and finding operations can be replaced directly
- Make review execution auditable:
  - every reviewer run stores `opencode_session_id`, raw log path, usage, status, duration, and error
  - report summary distinguishes real empty review from incomplete/failed review
  - no code path relies on prompt-path or DB fallback discovery for session IDs
- Make findings operational:
  - each finding has a stable `finding_id`
  - discussions, false-positive marks, severity changes, human non-fix labels, and feedback re-review outcomes append events against that ID
  - the final report remains a derived snapshot, not the sole source of truth
- Reuse [comain/unit-test-agent](https://github.com/comain/unit-test-agent) task operations surface:
  - dashboard/recent jobs
  - task progress
  - runner heartbeat/stale runner recovery
  - session/token/cost visibility
  - daemon CLI and terminal task dashboard pattern
- Reuse and harden the current CR context preparation:
  - construct deterministic diff artifacts before any LLM review
  - normalize CI/CI request metadata into a typed review context
  - adapt current review skills into prompt templates by reference, not by burying guidelines in Python strings
  - store the exact prompt inputs used by each reviewer run

## Non-Goals

- Do not port [comain/unit-test-agent](https://github.com/comain/unit-test-agent) Java/Python test generation, compile, coverage, mutation, or language-adapter logic.
- Do not replace `cr_agent` with [comain/unit-test-agent](https://github.com/comain/unit-test-agent) or make CR tasks depend on [comain/unit-test-agent](https://github.com/comain/unit-test-agent) domain model.
- Do not force per-file-only review like Alibaba's implementation.
- Do not introduce a cross-repo shared package in this version.
- Do not preserve index-based finding operations as an alternate compatibility path.
- Do not keep JSON task storage as the source of truth for v2 tasks.

## Tech Stack

- Python 3.9+.
- FastAPI and Jinja2 remain the HTTP/UI layer.
- SQLite becomes the durable task/review/finding/event store.
- LangGraph becomes the workflow orchestration layer.
- OpenCode remains the LLM execution substrate.
- [comain/unit-test-agent](https://github.com/comain/unit-test-agent) OpenCode routing/config code is copied/adapted for model selection, generated per-project `opencode.json`, provider fallback metadata, and llm-proxy configuration.
- CR v2 prompt assets under `src/cr_agent/review_v2/templates/` are the source review guideline content; the legacy scan skill tree has been removed.

## Commands

- Unit tests: `python -m pytest`
- Focused tests while implementing:
  - `python -m pytest tests/test_opencode_runner.py`
  - `python -m pytest tests/test_service.py`
  - `python -m pytest tests/test_routes.py`
  - add v2 tests under `tests/test_review_v2_*.py`
- Local app: `uvicorn cr_agent.app:app --host 0.0.0.0 --port 8000`
- Deployment command remains the existing production host supervisor/git-pull process until design changes it explicitly.

## Reuse Inventory From [comain/unit-test-agent](https://github.com/comain/unit-test-agent)

### Directly Reusable Or Adaptable

- `reference/opencode/process.py`
  - `OpenCodeProcessRunner.run_turn`
  - JSONL streaming via `opencode run --print-logs --format json`
  - process-group cleanup
  - raw turn logs under a stable cache directory
  - session ID capture from stream events
  - `TurnResult` status classification

- `reference/opencode/stream.py`
  - `OpenCodeStreamParser`
  - text extraction
  - token aggregation, including `input`, `output`, `reasoning`, `cache.read`, `cache.write`, and `total`
  - patch/tool counting
  - stop detection and rate-limit detection

- `reference/engine/llm_session.py`
  - active/idle/no-progress timeout policy
  - recoverable stall handling
  - guarded continue prompts
  - per-phase timeout multiplier pattern

- `reference/engine/session_usage.py`
  - shared token bucket shape:
    - `input`
    - `output`
    - `reasoning`
    - `cache_read`
    - `cache_write`
    - `total`

- `reference/engine/session_analysis.py`
  - multi-session token aggregation pattern
  - optional session retrospect pattern

- `reference/tasks/db.py`, `reference/tasks/models.py`, `reference/tasks/manager.py`
  - durable task statuses
  - task events
  - runner heartbeat and stale-runner recovery concepts
  - explicit `session_ids_json`
  - actual token and provider cost fields
  - provider/model routing metadata
  - dashboard/recent-job query support

- `reference/graph/state.py`, `reference/graph/workflow.py`
  - LangGraph `StateGraph` orchestration pattern
  - explicit state fields for session IDs, phase timings, token usage, task IDs, and current stage
  - deterministic conditional transitions

- `reference/api_trigger/store.py`
  - efficient recent-record summary loading pattern for large JSON task files

- [comain/unit-test-agent](https://github.com/comain/unit-test-agent) OpenCode config/routing modules:
  - copy/adapt model selection and provider-chain behavior
  - copy/adapt generated project `opencode.json` behavior
  - keep llm-proxy base URL/model selection visible in task config snapshots

- `reference/engine/diff.py`
  - copy/adapt language-neutral changed path and changed line helpers
  - use for deterministic changed-file and changed-line metadata instead of prompt-only discovery

- [comain/unit-test-agent](https://github.com/comain/unit-test-agent) context/prompt artifact patterns:
  - adapt the "write context files first, then pass paths to prompts" pattern
  - persist prompt inputs under the private audit directory for reproducibility

### Reuse As Reference Only

- `reference/graph/nodes.py`
  - many nodes are test-generation specific, but their phase split is useful:
    - deterministic setup
    - LLM planning
    - LLM execution
    - validation/repair
    - report/commit

## Proposed CR V2 Workflow

```text
trigger
  -> insert queued task into SQLite
  -> daemon claims queued task by lease
  -> prepare repository
  -> deterministic guard: repo/branch/commit/diff context must exist
  -> collect deterministic diff context
  -> normalize CI/CI request context
  -> write shared LLM context artifacts
  -> classify risk tier
  -> deterministic guard: reviewer plan must be non-empty or explicitly skipped as trivial
  -> build reviewer plan and reviewer prompt inputs
  -> run selected OpenCode reviewer sessions
  -> deterministic guard: every required reviewer must finish or mark task incomplete/failed
  -> persist reviewer run records
  -> deterministic guard: every run must have session ID and token usage or an explicit failure reason
  -> normalize and dedupe findings
  -> deterministic guard: normalized findings must have stable IDs and valid anchors
  -> persist finding records and events
  -> render derived report snapshot
  -> callback
```

Risk tiers:

- `trivial`: ignored-file-only diffs may be `skipped`; any production diff runs `correctness_light`
- `lite`: run correctness/code-quality and targeted specialists based on touched paths
- `full`: run correctness/code-quality, security, performance, API/contract, config/release, and coordinator/judge

The reviewer split should be deterministic at the workflow layer. The LLM can still reason inside each reviewer, but it should not be responsible for deciding whether the task was reviewed, which reviewers ran, or whether missing outputs are acceptable.

## Deterministic Guards

The workflow must include hard checks before advancing stages:

- repository workspace exists and is on the intended branch/commit
- diff range is resolved and changed-file metadata is written
- CI/CI request context is normalized and persisted
- review guideline file paths exist and are readable
- shared context artifacts and reviewer prompt inputs are written before OpenCode starts
- risk tier and reviewer plan are written before reviewer execution
- non-trivial tasks have at least one required reviewer
- every required reviewer produces a reviewer-run row
- successful reviewer runs include `opencode_session_id`, raw log path, output path, token bucket, and duration
- reviewer output parses into a typed result or the reviewer is marked failed
- judge/coordinator cannot convert missing reviewer output into "no issues"
- empty findings are only valid when all required reviewer runs succeeded
- callback/report generation refuses to label incomplete review as success
- stale runners/tasks are detected by heartbeat and recovered or marked failed
- trigger APIs do not run OpenCode inline; they return after durable enqueue
- daemon task claiming is atomic and lease-based, so two daemon processes cannot run the same task

## Proposed Data Model

### Reviewer Run

Each OpenCode child session should produce one durable run record:

```json
{
  "review_run_id": "rr_...",
  "task_id": "...",
  "reviewer": "security",
  "risk_tier": "full",
  "opencode_session_id": "ses_...",
  "model": "tokenpool/gpt-5.5",
  "status": "success",
  "started_at": "...",
  "finished_at": "...",
  "duration_seconds": 91.2,
  "raw_log_path": "runtime/audit/<task_id>/reviewers/security/raw.jsonl",
  "output_path": "runtime/audit/<task_id>/reviewers/security/output.json",
  "usage": {
    "input_tokens": 120000,
    "output_tokens": 8000,
    "reasoning_tokens": 3000,
    "cache_read_tokens": 90000,
    "cache_write_tokens": 12000,
    "total_tokens": 143000,
    "cost_usd": 0.1847
  },
  "findings_count": 3,
  "error": null
}
```

### Finding

Findings should be first-class records:

```json
{
  "finding_id": "f_...",
  "task_id": "...",
  "source_reviewer": "security",
  "source_review_run_id": "rr_...",
  "opencode_session_id": "ses_...",
  "status": "open",
  "resolution": null,
  "severity": "high",
  "category": "security",
  "confidence": 0.86,
  "title": "...",
  "detail": "...",
  "suggestion": "...",
  "file": "src/foo.py",
  "line": 42,
  "end_line": 48,
  "line_anchor": {
    "commit": "...",
    "diff_hunk": "@@ ...",
    "code_hash": "sha256:..."
  },
  "dedupe_key": "sha256:..."
}
```

### Finding Event

All user/model operations should append events:

```json
{
  "event_id": "fe_...",
  "task_id": "...",
  "finding_id": "f_...",
  "type": "user_comment",
  "actor": "user",
  "body": "...",
  "metadata": {},
  "created_at": "..."
}
```

Initial event types:

- `finding_created`
- `user_comment`
- `model_reply`
- `severity_changed`
- `marked_false_positive`
- `reopened`
- `marked_human_non_fix`
- `feedback_session_started`
- `feedback_session_completed`
- `feedback_session_failed`
- `resolved_by_re_review`
- `missed_issue_reported`
- `missed_issue_confirmed`
- `missed_issue_rejected`
- `status_changed`

## Storage Strategy

Use SQLite as the source of truth in this version. Public JSON/HTML files under the report directory are sanitized generated artifacts for report rendering. Raw debug/reproducibility artifacts live under the private audit directory.

SQLite owns:

- task records and status
- reviewer run records
- finding records
- finding events
- feedback sessions
- token/cost fields
- runner heartbeats
- task events
- dashboard/recent job queries

Generated artifacts remain useful:

```text
runtime/reports/<task_id>/
  index.html
  result.json                  # sanitized derived report data

runtime/audit/<task_id>/
  context/
    diff.patch
    changed_files.json
    changed_lines.json
    ci_request.json
    llm_context.json
    risk.json
    reviewer_plan.json
    prompt_references.json
  reviewers/
    <reviewer>/
      prompt.md
      prompt_inputs.json
      raw.jsonl
      output.json
```

[comain/unit-test-agent](https://github.com/comain/unit-test-agent) `TaskDB` is the schema reference, but CR should use CR-specific table names and fields rather than importing [comain/unit-test-agent](https://github.com/comain/unit-test-agent) class/test terminology.

## LLM Context Preparation

V2 should introduce a dedicated context-preparation layer before reviewer execution. This replaces the current coupling where `GitClient.collect_review_context` and `OpencodeRunner._build_prompt` jointly construct context inside the one-shot runner.

Required context artifacts:

- `diff.patch`: full bounded diff for the selected range, with size limits decided in design
- `changed_files.json`: all changed files plus filtered production/non-test files
- `changed_lines.json`: added/changed line numbers by file using unit-test-agent-style diff parsing
- `ci_request.json`: normalized trigger metadata from CI/manual CI request
- `llm_context.json`: task-level summary used by every reviewer
- `prompt_references.json`: resolved prompt reference file names and versions
- `reviewer_plan.json`: selected reviewers, risk tier, required/optional flags, and prompt template names
- `reviewers/<reviewer>/prompt_inputs.json`: exact structured inputs for one reviewer
- `reviewers/<reviewer>/prompt.md`: final prompt sent to OpenCode

Raw context and reviewer artifacts must be written under the private audit directory, not under the mounted report directory.

The prompt builder should use templates, not ad hoc long strings inside the runner. Templates should:

- reference the packaged CR v2 prompt reference asset, `src/cr_agent/review_v2/templates/references/review.md`
- include deterministic task context: repo path, branch, commit, diff range, changed files, changed lines, diff stat, commit log, and CI/CI metadata
- include reviewer role/category and expected JSON schema
- instruct the model to inspect only relevant non-test changes unless cross-file impact is justified
- require Chinese user-facing finding text while keeping JSON keys stable
- keep raw diff/context paths available so OpenCode can read them when the inline summary is insufficient

Prompt templates and CR v2 prompt assets live under `src/cr_agent/review_v2/templates/` as package data so the worker and deployed service resolve the same files. CR v2 guidelines, reviewer personas, and prompt references are packaged there instead of being split across repository roots.

## Current `cr_agent` Integration Points

- `src/cr_agent/core/service.py`
  - owns trigger API orchestration and task enqueueing
  - v2 should stop running review work inline; queued tasks are executed by the daemon

- `src/cr_agent/core/opencode_runner.py`
  - currently owns prompt creation, command execution, output repair, parsing, and usage recording
  - v2 should replace the execution layer with unit-test-agent-style `OpenCodeProcessRunner`
  - v2 should reuse/adapt [comain/unit-test-agent](https://github.com/comain/unit-test-agent) OpenCode config generation and model-selection/routing logic
  - v2 should remove context construction from the runner and consume prebuilt prompt files/inputs

- `src/cr_agent/core/usage.py`
  - currently records coarse usage rows
  - v2 should record per-reviewer usage from stream tokens into SQLite first

- `src/cr_agent/core/storage.py`
  - current JSON `TaskStore` should be replaced for v2 task execution
  - any remaining JSON output should be a derived artifact

- `src/cr_agent/core/feedback.py`
  - current `finding_index` threads should be replaced by stable `finding_id` events

- `src/cr_agent/models.py`
  - add v2 models for reviewer runs, finding records, and finding events
  - `AnalysisResult` and `FindingView` can remain as derived compatibility payloads for report/callback output, not as storage source of truth

- `src/cr_agent/api/routes.py`
  - report detail should be backed by SQLite source-of-truth records
  - recent jobs should reuse/adapt [comain/unit-test-agent](https://github.com/comain/unit-test-agent) dashboard/task progress patterns and include reviewer/session/usage status

- `src/cr_agent/review_v2/daemon.py` and `src/cr_agent/review_v2/cli.py`
  - daemon claims queued tasks, updates heartbeat/lease state, runs `WorkflowRunner`, retries callbacks, and recovers stale tasks
  - CLI exposes `run`, `once`, `recover-stale`, `retry-callbacks`, `status`, and `dashboard`

- `src/cr_agent/core/git_client.py`
  - current `collect_review_context` is the starting point for v2 context preparation
  - v2 should expand it with full diff, changed lines, normalized CI/CI context, and persisted artifacts

- `src/cr_agent/review_v2/templates/`
  - current CR v2 prompt source of truth
  - contains Jinja prompt templates plus Markdown reference files under `references/`
  - v2 prompt templates should record these package asset names in `prompt_references.json`

## Project Structure

Expected v2 module layout:

```text
src/cr_agent/review_v2/
  state.py              # LangGraph state and typed workflow state helpers
  workflow.py           # StateGraph definition and deterministic transitions
  nodes.py              # CR-specific workflow nodes
  guards.py             # deterministic guard checks
  context.py            # diff/request/guideline context artifact preparation
  prompts.py            # prompt input assembly and template rendering
  opencode_process.py   # copied/adapted [comain/unit-test-agent](https://github.com/comain/unit-test-agent) OpenCode process runner
  opencode_stream.py    # copied/adapted [comain/unit-test-agent](https://github.com/comain/unit-test-agent) stream parser
  opencode_config.py    # copied/adapted config generation/model selection
  storage.py            # SQLite schema/repositories
  models.py             # reviewer run/finding/event DTOs
  dashboard.py          # task dashboard query helpers
  daemon.py             # SQLite queue claimant and task runner
  cli.py                # daemon CLI and terminal dashboard
  templates/
    reviewer.md.j2      # reviewer prompt template
    judge.md.j2         # coordinator/judge prompt template
```

Tests should mirror these modules under `tests/`.

## Code Style

Keep CR-specific naming in copied/adapted [comain/unit-test-agent](https://github.com/comain/unit-test-agent) code. Do not leak [comain/unit-test-agent](https://github.com/comain/unit-test-agent) test-generation terms such as `class_task`, `coverage`, or `mutation` into CR tables or public APIs.

Example storage API shape:

```python
run = store.create_reviewer_run(
    task_id=task_id,
    reviewer="security",
    risk_tier="full",
    model=selected_model,
)
store.finish_reviewer_run(
    review_run_id=run.review_run_id,
    status="success",
    opencode_session_id=result.session_id,
    usage=result.tokens,
    raw_log_path=result.raw_log_path,
)
```

## Testing Strategy

- Unit-test copied/adapted OpenCode stream parsing with saved JSONL fixtures.
- Unit-test context preparation for diff range, changed files, changed lines, CI/CI metadata, guideline path resolution, and prompt input persistence.
- Snapshot-test prompt rendering for at least correctness and security reviewers.
- Unit-test deterministic guards for missing diff, missing reviewer plan, missing session ID, failed reviewer, invalid empty result, and stale heartbeat.
- Unit-test SQLite repositories with temporary databases and WAL enabled.
- Unit-test LangGraph routing with fake nodes and controlled failures.
- Route tests should verify recent jobs, progress, report detail, finding events, and callback payload generation from SQLite.
- Integration-style tests should run the workflow with a fake OpenCode runner before any real OpenCode test is added.

## Acceptance Criteria

- A CR task can run through the v2 LangGraph workflow and produce usable report/callback output.
- SQLite is the source of truth for task status, reviewer runs, findings, finding events, feedback sessions, usage, and dashboard data.
- Trigger APIs enqueue durable SQLite tasks and return task/report/progress URLs without running OpenCode inline.
- The CR daemon claims queued tasks atomically by lease, and two daemon processes cannot run the same task concurrently.
- The daemon CLI exposes run/once/recover-stale/retry-callbacks/status/dashboard operations with worker identity, concurrency, polling, claim-limit, lease, once/run-forever, stale recovery, and callback retry parameters.
- The terminal dashboard shows queue counts, active daemon heartbeat, active task stages, callback retry state, token/cost totals, and recent failures without mutating task state.
- Each selected reviewer has one current successful reviewer-run attempt for the active workflow run, while failed/retried attempts remain stored with explicit attempt number, session ID when available, usage, status, duration, raw log path, and output path.
- If any required reviewer fails or returns invalid output, the task must not be reported as a clean pass.
- Empty findings are only shown as real empty when all required reviewers finished successfully and the judge/coordinator accepted the empty result.
- A successful `passed` task with production changed files must have at least one successful required reviewer run.
- Ignored-file-only tasks may use `gate_status=skipped`, and reports/callbacks must label them as skipped rather than clean reviewed.
- Token usage includes cache-read and cache-write tokens, and the UI can show a single total cost plus total tokens.
- Findings are addressed by stable `finding_id`; old index-based operations are removed.
- Discussion, false-positive, severity-change, human non-fix labels, and feedback re-review operations append finding events.
- The workflow records enough stage/reviewer state for recent jobs and progress pages to explain where time was spent.
- [comain/unit-test-agent](https://github.com/comain/unit-test-agent) OpenCode config generation and model-selection/routing behavior is reused/adapted.
- Context preparation is deterministic and persisted before reviewer sessions start.
- Prompt templates reference current CR review guidelines and store exact prompt inputs for each reviewer run.
- [comain/unit-test-agent](https://github.com/comain/unit-test-agent) dashboard/recent-job/progress patterns are reused/adapted for CR tasks.
- Deterministic guards prevent missing reviewer output from becoming a successful empty report.
- Public report files are sanitized; raw audit artifacts are stored outside the mounted report root.

## Open Questions Before Design

- Closed by design: reviewer sets, bounded parallelism, production host drain/rollout, old static report readability, template location, private audit artifacts, and [comain/unit-test-agent](https://github.com/comain/unit-test-agent) source-module mapping.

## Design Approval Gate

If this spec is approved, the design phase should produce:

- module-level design for `cr_agent.review_v2`
- exact reuse/copy list from [comain/unit-test-agent](https://github.com/comain/unit-test-agent)
- SQLite schema and migration/rollback plan
- direct replacement plan for current JSON task store and index-based feedback
- context-preparation design covering diff construction, CI/CI request normalization, guideline resolution, and prompt template rendering
- test plan with unit tests for stream parsing, task status, finding events, and report compatibility
- rollout plan for replacing v1 on production host, including dashboard/progress verification and failure rollback

# Implementation Plan: CR V2 Cloudflare-Style Review Orchestration

Spec: `docs/spec-cr-v2-cloudflare-reuse.md`
Design: `docs/design-cr-v2-cloudflare-reuse.md`
Ticket: N/A. This is confirmed non-Ticket tool work, so `docs/` artifacts are used instead of Ticket-keyed `doc/` artifacts.
Release approval evidence: N/A unless this work is later attached to a Ticket/CI release. If that happens, add `docs/release-approval-<JIRA>.evidence.json` and run `release-approval command release approval status --ticket <JIRA>` before `/ship`.
Java guideline verification: N/A. This repo is a Python/FastAPI service, not a Java project.

## Overview

Replace the current one-shot CR review path with a SQLite-backed, LangGraph-orchestrated review system that deterministically prepares context, selects bounded reviewer sessions, records OpenCode session IDs and token usage at run time, judges/normalizes findings, renders sanitized reports, supports stable `finding_id` feedback operations, and retires v1 JSON/index/fix-session behavior for new tasks.

## Architecture Decisions

- SQLite is the authoritative store for tasks, reviewer runs, findings, finding events, feedback sessions, usage, heartbeats, and dashboard/recent-job data.
- `cr_agent.review_v2` owns the new workflow, storage, queue/daemon, context, prompt, OpenCode, finding, feedback, and dashboard code. Existing service/routes become thin adapters that enqueue tasks and read SQLite state.
- Runtime modules are copied/adapted directly from the public [comain/unit-test-agent](https://github.com/comain/unit-test-agent) repo; no shared cross-repo package is introduced.
- Reuse-first is mandatory: when [comain/unit-test-agent](https://github.com/comain/unit-test-agent) already has equivalent language-neutral behavior, implementation must copy/adapt it from the design's [comain/unit-test-agent](https://github.com/comain/unit-test-agent) Reuse Source Map instead of rebuilding. A rebuild is allowed only when the implementation note explains the incompatible [comain/unit-test-agent](https://github.com/comain/unit-test-agent) assumption and the CR-specific replacement.
- LangGraph contains coarse orchestration nodes: `prepare_review`, `run_reviewers`, `judge_findings`, `finalize_task`, and `fail_task`.
- CR v2 is read-only. Fix-session code is removed from v2 behavior.
- Public reports are generated snapshots. Raw diffs, prompts, prompt inputs, raw JSONL, and provider snapshots stay in private audit storage.
- Two Critical design-review findings are accepted as not fixed in this version: legacy JSON-backed report compatibility and a new auth layer for human resolution/pass acknowledgment. Old static reports remain best-effort through `StaticFiles`; human resolution relies on the existing internal deployment/report URL boundary.

## Dependency Graph

```text
Accepted not-fix boundaries
  -> schema/repository foundation
      -> daemon queue/CLI foundation
          -> [comain/unit-test-agent](https://github.com/comain/unit-test-agent) OpenCode runtime + model routing
          -> context/prompt artifacts
              -> daemon-driven skipped/light workflow
                  -> full reviewer/judge workflow
                      -> report/recent/progress routes
                          -> feedback/resolution sessions
                              -> v1 code retirement
                                  -> rollout/production host verification
```

Parallelizable after Task 3:

- Context/prompt implementation can proceed in parallel with report/dashboard DTO work once schema models are stable.
- OpenCode stream/routing tests can proceed in parallel with SQLite repository tests.
- Documentation/README updates can proceed after route/report contracts are stable.

## Task List

### Phase 0: Scope Gates

#### Task 1: Record Accepted Not-Fix Boundaries

**Description:** Lock the human decision that legacy JSON-backed report compatibility and new auth for human resolution are not fixed in this version, so implementation does not keep accidental compatibility paths or block on a new auth subsystem.

**Acceptance criteria:**
- [ ] Design review dispositions say both Critical findings are accepted as not fixed in this version.
- [ ] Plan does not contain rollout/auth gates for these items.
- [ ] Implementation tasks explicitly avoid legacy JSON detail adapters and new auth infrastructure unless a later design revision changes the decision.

**Verification:**
- [ ] A stale-wording scan confirms the docs do not present the two Critical findings as unresolved gates.
- [ ] Requirement coverage maps both not-fix decisions to explicit non-work.

**Dependencies:** None.

**Files likely touched:**
- `docs/spec-cr-v2-cloudflare-reuse.md`
- `docs/design-cr-v2-cloudflare-reuse.md`
- Possibly `docs/decisions/ADR-005-*.md`

**Estimated scope:** S.

### Phase 1: Foundations

#### Task 2: Add V2 Configuration And Dependencies

**Description:** Add LangGraph dependency, package data for v2 templates, and settings needed by SQLite, audit/report directories, concurrency, context limits, callback retry, and [comain/unit-test-agent](https://github.com/comain/unit-test-agent) model routing.

**Acceptance criteria:**
- [ ] `langgraph` is installed and importable in tests.
- [ ] Settings include SQLite DB path, audit dir, daemon ID/concurrency/polling/lease settings, reviewer concurrency, global OpenCode concurrency, context/log limits, callback retry fields, and [comain/unit-test-agent](https://github.com/comain/unit-test-agent) provider-chain/model-routing settings.
- [ ] Generated config uses `http://openai-compatible.example.com/v1` for llm-proxy and never writes `apiKey`.
- [ ] Hacioded secret defaults such as GitLab token defaults are removed or moved to env-only configuration.

**Verification:**
- [ ] `python -m pytest tests/test_config*.py tests/test_review_v2_opencode_routing.py`
- [ ] Inspect generated `opencode.json` fixture for no token values.

**Dependencies:** Task 1.

**Files likely touched:**
- `pyproject.toml`
- `src/cr_agent/config.py`
- `config/env/*.env`
- `tests/test_config_defaults.py`
- `tests/test_review_v2_opencode_routing.py`

**Estimated scope:** M.

#### Task 3: Build SQLite ReviewDB And Repositories

**Description:** Create `review_v2.storage` with idempotent schema init, WAL/foreign-key/busy-timeout setup, short transaction helpers, and repositories for tasks, reviewer plans, reviewer runs, findings, events, feedback sessions, task events, heartbeats, callback retry state, and usage aggregation.

**Acceptance criteria:**
- [ ] Schema covers `cr_tasks`, `reviewer_plans`, `reviewer_runs`, `findings`, `finding_events`, `feedback_sessions`, `task_events`, and `runner_heartbeats`.
- [ ] `cr_tasks` includes queue/lease fields: priority, queued time, not-before time, claimed daemon, lease expiry, and last heartbeat.
- [ ] Indexes match the design, including task-level event indexes and reviewer run workflow/attempt indexes.
- [ ] Reviewer retries are represented by `workflow_run_id` and `attempt`; report queries can select the current successful attempt.
- [ ] Severity persists as `fatal|high|medium|low|info`, with `critical` normalized to `fatal`.
- [ ] Token/cost buckets include input, output, reasoning, cache-read, cache-write, total, and cost.

**Verification:**
- [ ] `python -m pytest tests/test_review_v2_storage.py tests/test_review_v2_severity.py`
- [ ] Temporary DB tests assert WAL, foreign keys, rollback, indexes, and enum checks.

**Dependencies:** Task 2.

**Files likely touched:**
- `src/cr_agent/review_v2/storage.py`
- `src/cr_agent/review_v2/models.py`
- `tests/test_review_v2_storage.py`
- `tests/test_review_v2_severity.py`

**Estimated scope:** M.

#### Task 4: Copy/Adapt [comain/unit-test-agent](https://github.com/comain/unit-test-agent) OpenCode Runtime And Routing

**Description:** Copy/adapt [comain/unit-test-agent](https://github.com/comain/unit-test-agent) OpenCode process, stream parser, config generator, tiered routing, session usage, session analysis, and LLM stall recovery into CR v2 with CR naming and no [comain/unit-test-agent](https://github.com/comain/unit-test-agent) test-generation terminology.

**Acceptance criteria:**
- [ ] Every copied/adapted module records its [comain/unit-test-agent](https://github.com/comain/unit-test-agent) source path in the module docstring or adjacent implementation note.
- [ ] Any new implementation that overlaps [comain/unit-test-agent](https://github.com/comain/unit-test-agent) source-map behavior includes a short justification for why direct copy/adapt was not possible.
- [ ] `opencode_process` runs `opencode run --print-logs --format json`, captures raw JSONL, session ID, tokens, duration, patch/tool counts, and errors.
- [ ] `opencode_stream` parses input/output/reasoning/cache-read/cache-write/total tokens from saved JSONL fixtures.
- [ ] `opencode_config` writes per-project `opencode.json`, backs up/restores any existing config, deletes generated config when no original existed, excludes it from review context, and asserts cleanup.
- [ ] `opencode_routing` supports provider-chain parsing, model probe/cooldown, fallback history, selected provider/model snapshots, and redacted token status.
- [ ] Process groups terminate on timeout/cancel; no-output/rate-limit/provider errors are classified.

**Verification:**
- [ ] `python -m pytest tests/test_review_v2_opencode_stream.py tests/test_review_v2_opencode_routing.py tests/test_review_v2_opencode_process.py`
- [ ] Fixture tests compare unit-test-agent-compatible token bucket output.

**Dependencies:** Task 2.

**Files likely touched:**
- `src/cr_agent/review_v2/opencode_process.py`
- `src/cr_agent/review_v2/opencode_stream.py`
- `src/cr_agent/review_v2/opencode_config.py`
- `src/cr_agent/review_v2/opencode_routing.py`
- `src/cr_agent/review_v2/llm_session.py`
- `src/cr_agent/review_v2/session_usage.py`
- `src/cr_agent/review_v2/session_analysis.py`
- `tests/test_review_v2_opencode_*.py`

**Estimated scope:** M.

### Checkpoint: Foundation

- [ ] `python -m pytest tests/test_review_v2_storage.py tests/test_review_v2_opencode_stream.py tests/test_review_v2_opencode_routing.py`
- [ ] No generated config contains `apiKey` or provider token values.
- [ ] Schema and routing fields match the design coverage matrix.
- [ ] Human review before wiring TaskService to v2.

### Phase 2: Review Execution

#### Task 5: Implement Deterministic Context And Prompt Preparation

**Description:** Build `ContextBuilder`, `ArtifactStore`, and `PromptRenderer` so review context is prepared before any LLM call and prompt templates reference current CR guidelines.

**Acceptance criteria:**
- [ ] Context artifacts are written under private `audit_dir/<task_id>/context`.
- [ ] Artifacts include bounded `diff.patch`, `changed_files.json`, `changed_lines.json`, `ci_request.json`, `llm_context.json`, `prompt_references.json`, `risk.json`, and `reviewer_plan.json`.
- [ ] Risk tier and reviewer plan are deterministic using the design thresholds and specialist triggers.
- [ ] Reviewer `prompt_inputs.json` and `prompt.md` are written before OpenCode starts.
- [ ] Prompt templates and CR v2 prompt assets live under `src/cr_agent/review_v2/templates/`; legacy scan skills are removed.
- [ ] Context-too-large and ignored-file-only guards produce explicit task outcomes.

**Verification:**
- [ ] `python -m pytest tests/test_review_v2_context.py tests/test_review_v2_risk_plan.py tests/test_review_v2_prompts.py tests/test_review_v2_limits.py`
- [ ] Snapshot tests cover correctness, security, and judge prompts.

**Dependencies:** Tasks 2 and 3.

**Files likely touched:**
- `src/cr_agent/review_v2/context.py`
- `src/cr_agent/review_v2/artifacts.py`
- `src/cr_agent/review_v2/prompts.py`
- `src/cr_agent/review_v2/guards.py`
- `src/cr_agent/review_v2/templates/reviewer.md.j2`
- `src/cr_agent/review_v2/templates/judge.md.j2`
- `src/cr_agent/core/git_client.py`
- `tests/test_review_v2_context.py`
- `tests/test_review_v2_risk_plan.py`
- `tests/test_review_v2_prompts.py`
- `tests/test_review_v2_limits.py`

**Estimated scope:** M.

#### Task 6: Wire Daemon-Driven Skipped/Light Path End To End

**Description:** Implement the initial path where trigger APIs enqueue SQLite tasks, the daemon claims queued tasks by lease, and `WorkflowRunner` runs skipped/light review with fake OpenCode support, report generation, and callback payload creation.

**Acceptance criteria:**
- [ ] Queue claim, heartbeat, event, and stale-recovery code is adapted from [comain/unit-test-agent](https://github.com/comain/unit-test-agent) `reference/tasks/db.py`, `reference/tasks/manager.py`, and `reference/tasks/scheduler.py`; any rebuilt part has an implementation note explaining the CR-only difference.
- [ ] `TaskService.submit()` creates a SQLite task with `status=queued` and returns task/report/progress URLs without running OpenCode inline.
- [ ] `CRReviewDaemon.once()` atomically claims an eligible queued task and invokes `WorkflowRunner`.
- [ ] Two daemon instances cannot claim the same task.
- [ ] Ignored-file-only diffs produce `status=success`, `gate_status=skipped`, a sanitized report, and skipped callback semantics.
- [ ] Light production diffs run required `correctness_light`, store reviewer run session/usage/log/output paths, pass through judge, and render report/callback.
- [ ] Missing session ID, failed required reviewer, invalid reviewer output, or invalid empty result cannot reach `gate_status=passed`.
- [ ] Stage/task events explain current progress.

**Verification:**
- [ ] `python -m pytest tests/test_review_v2_workflow.py tests/test_review_v2_guards.py tests/test_review_v2_daemon.py tests/test_service.py`
- [ ] Fake runner integration produces public report artifacts without real OpenCode.

**Dependencies:** Tasks 3, 4, and 5.

**Files likely touched:**
- `src/cr_agent/review_v2/state.py`
- `src/cr_agent/review_v2/workflow.py`
- `src/cr_agent/review_v2/nodes.py`
- `src/cr_agent/review_v2/guards.py`
- `src/cr_agent/core/service.py`
- `tests/test_review_v2_workflow.py`
- `tests/test_review_v2_guards.py`
- `tests/test_service.py`

**Estimated scope:** M.

#### Task 7: Implement Full Reviewer Fanout And Judge Normalization

**Description:** Complete standard/full risk execution with bounded reviewer concurrency, optional/required reviewer semantics, judge precision/recall logic, stable finding IDs, dedupe, anchors, and accepted-empty safeguards.

**Acceptance criteria:**
- [ ] Required and optional reviewers follow deterministic reviewer-plan rules and global OpenCode semaphore.
- [ ] Required reviewer failures block judge/final pass; optional failures are visible but non-blocking.
- [ ] Judge validates reviewer JSON, preserves candidates with confidence >= 0.3 for recall, applies evidence/dedupe/precision passes, writes private rejections, and requires accepted-empty rationale for high-risk empty output.
- [ ] Findings are persisted with stable `finding_id`, dedupe key, source reviewer/run/session metadata, anchors, severity, confidence, and Chinese user-facing text.
- [ ] Full-risk empty or all-rejected output without accepted-empty rationale becomes incomplete/failed.

**Verification:**
- [ ] `python -m pytest tests/test_review_v2_judge.py tests/test_review_v2_workflow.py tests/test_review_v2_guards.py`
- [ ] Concurrency test proves per-task max 3 and process max 4 OpenCode runs.

**Dependencies:** Task 6.

**Files likely touched:**
- `src/cr_agent/review_v2/nodes.py`
- `src/cr_agent/review_v2/findings.py`
- `src/cr_agent/review_v2/workflow.py`
- `src/cr_agent/review_v2/guards.py`
- `tests/test_review_v2_judge.py`
- `tests/test_review_v2_workflow.py`

**Estimated scope:** M.

#### Task 8: Implement Daemon CLI, Callback Retry, Heartbeats, And Recovery

**Description:** Add daemon run/once/recover/retry/status/dashboard CLI, callback retry state machine, daemon heartbeats, lease extension/release, stale-running-task recovery, and idempotent callback behavior around terminal review state.

**Acceptance criteria:**
- [ ] Daemon loop, signal handling, worker concurrency, child cleanup/requeue, stale recovery, and dashboard rendering are adapted from [comain/unit-test-agent](https://github.com/comain/unit-test-agent) `reference/cli.py` `tasks daemon` / `tasks dashboard` and `scripts/start_daemon.sh`; rebuilt parts have implementation notes.
- [ ] CLI supports `run`, `once`, `recover-stale`, `retry-callbacks`, `status`, and `dashboard`.
- [ ] CLI accepts daemon ID, concurrency, poll interval, claim limit, lease seconds, once/run-forever mode, stale recovery, and callback retry options.
- [ ] Terminal dashboard is read-only and shows queued/running/success/failed counts, active daemon heartbeat, active task stages, callback state, token/cost totals, and recent failures.
- [ ] `finalize_task` writes terminal review/report state before callback attempt.
- [ ] Callback failure stores redacted history, attempt count, next retry, and last error without changing review outcome.
- [ ] Retry worker uses bounded exponential backoff and stops at configured attempts unless manually retried.
- [ ] Daemon heartbeats and task leases update while tasks run; stale tasks are requeued or failed according to policy.
- [ ] Recent/progress output exposes callback state and current stage.

**Verification:**
- [ ] `python -m pytest tests/test_review_v2_callback_retry.py tests/test_review_v2_daemon.py tests/test_review_v2_cli.py tests/test_service.py tests/test_routes.py`
- [ ] Simulated callback failure keeps `gate_status=passed|failed|skipped` unchanged and shows `callback_state=retrying|failed`.

**Dependencies:** Tasks 3 and 6.

**Files likely touched:**
- `src/cr_agent/core/callback.py`
- `src/cr_agent/core/service.py`
- `src/cr_agent/review_v2/daemon.py`
- `src/cr_agent/review_v2/cli.py`
- `src/cr_agent/review_v2/storage.py`
- `src/cr_agent/review_v2/workflow.py`
- `tests/test_review_v2_callback_retry.py`
- `tests/test_review_v2_daemon.py`
- `tests/test_review_v2_cli.py`
- `tests/test_service.py`

**Estimated scope:** M.

### Checkpoint: Execution

- [ ] `python -m pytest tests/test_review_v2_workflow.py tests/test_review_v2_judge.py tests/test_review_v2_callback_retry.py`
- [ ] Fake-runner E2E covers skipped, light pass, failed required reviewer, invalid judge, and callback failure.
- [ ] No production diff can produce a clean pass without a required reviewer session.
- [ ] Human review before replacing routes/report UI.

### Phase 3: Product Surfaces

#### Task 9: Replace Report, Detail, Recent, And Progress Routes With SQLite-Backed V2 Views

**Description:** Update API routes, report writer, templates, recent jobs, progress, and task status to read SQLite records and generated artifacts rather than JSON task files or OpenCode DB fallback.

**Acceptance criteria:**
- [ ] `/reports/recent.html` and `/reports/recent/data` show recent CR tasks, stages, status/gate status, reviewer counts, session IDs, callback state, and compact total cost/tokens.
- [ ] `/task-status/{task_id}` and `/task-status/{task_id}/data` read SQLite task/reviewer/finding summaries.
- [ ] `/reports/{task_id}/detail` returns `ReportDetailV2` with findings, events, general feedbacks, review sessions, feedback sessions, usage, cost, and reviewer runs.
- [ ] Public `index.html` shows review-session timeline, combined token display like `$0.1847 · 151.6K tokens`, and no raw private paths.
- [ ] Public artifacts contain no raw diff, prompts, prompt inputs, raw JSONL, provider config snapshots, or secrets.

**Verification:**
- [ ] `python -m pytest tests/test_routes.py tests/test_review_v2_report_sessions.py tests/test_review_v2_artifacts.py tests/test_review_v2_secrets.py`
- [ ] Browser/manual check of recent page, progress page, and generated report with a fake-runner task.

**Dependencies:** Tasks 3, 6, 7, and 8.

**Files likely touched:**
- `src/cr_agent/api/routes.py`
- `src/cr_agent/core/reporting.py`
- `src/cr_agent/templates/report.html.j2`
- `src/cr_agent/review_v2/dashboard.py`
- `src/cr_agent/review_v2/artifacts.py`
- `tests/test_routes.py`
- `tests/test_review_v2_report_sessions.py`
- `tests/test_review_v2_artifacts.py`
- `tests/test_review_v2_secrets.py`

**Estimated scope:** M.

#### Task 10: Implement Finding Feedback And Resolution Sessions

**Description:** Replace index-based feedback with stable `finding_id` operations, standalone feedback OpenCode sessions, finding events, combined token aggregation, and task pass/ack recomputation when all findings resolve.

**Acceptance criteria:**
- [ ] `POST /reports/{task_id}/findings/{finding_id}/feedback` appends user comment, creates `feedback_sessions`, runs a simplified read-only feedback review, appends model reply, and applies exactly one allowed action.
- [ ] `POST /reports/{task_id}/general-feedback` creates `feedback_sessions(finding_id=NULL)`, runs missed-issue confirmation, and appends confirmed/rejected events.
- [ ] Human resolution route follows the accepted not-fix boundary from Task 1: no new auth layer in this version; pass/ack recomputation is still implemented and risk is documented.
- [ ] All feedback sessions store parent review/session links, own OpenCode session ID, prompt paths, raw log path, usage, status, error, and result action.
- [ ] When every finding is resolved by re-review pass, human non-fix, or false-positive, eligible reviewed tasks transition to `success/passed`, regenerate report, and resend idempotent callback; incomplete/context-failed/cancelled tasks cannot be converted.
- [ ] Report detail and header usage include completed feedback subsession token/cost totals.

**Verification:**
- [ ] `python -m pytest tests/test_review_v2_feedback_sessions.py tests/test_review_v2_resolution_ack.py tests/test_review_v2_report_sessions.py tests/test_routes.py`
- [ ] Manual fake-runner check: finding comment creates a visible child session and updates combined usage.

**Dependencies:** Tasks 1, 4, 7, 8, and 9.

**Files likely touched:**
- `src/cr_agent/review_v2/feedback.py`
- `src/cr_agent/review_v2/templates/feedback_review.md.j2`
- `src/cr_agent/review_v2/storage.py`
- `src/cr_agent/api/routes.py`
- `src/cr_agent/templates/report.html.j2`
- `tests/test_review_v2_feedback_sessions.py`
- `tests/test_review_v2_resolution_ack.py`

**Estimated scope:** M.

#### Task 11: Retire V1 JSON, Index, Fix-Session, And DB-Fallback Code

**Description:** Remove the v1 code paths listed in the design removal contract after v2 routes/workflow are green, keeping only explicitly allowed temporary adapters.

**Acceptance criteria:**
- [ ] New tasks no longer import or instantiate JSON `TaskStore` or write `runtime/tasks/*.json`.
- [ ] `FeedbackStore`, report-local `feedback.json`, index-based threads, and `/findings/{finding_index}/feedback` are removed from v2 behavior.
- [ ] Fix-session models, routes, UI controls, service workers, Git fix workspace, MR creation, and OpenCode fix prompts are removed or isolated outside v2 with no route exposure.
- [ ] OpenCode DB fallback functions are not used by v2 usage/session accounting.
- [ ] `AnalysisResult` is not the source of truth for v2 storage; it remains only if needed as a derived compatibility/report payload.

**Verification:**
- [ ] `rg "TaskStore|FeedbackStore|FixSession|finding_index|_load_usage_metrics_from_opencode_db|fix-sessions" src tests` shows only allowed legacy/fixture references.
- [ ] `python -m pytest`.

**Dependencies:** Tasks 9 and 10.

**Files likely touched:**
- `src/cr_agent/core/storage.py`
- `src/cr_agent/core/feedback.py`
- `src/cr_agent/core/service.py`
- `src/cr_agent/core/opencode_runner.py`
- `src/cr_agent/core/git_client.py`
- `src/cr_agent/models.py`
- `src/cr_agent/api/routes.py`
- `src/cr_agent/templates/report.html.j2`
- `tests/test_*.py`

**Estimated scope:** M.

### Checkpoint: Product Surfaces

- [ ] `python -m pytest`.
- [ ] All v2 public routes use SQLite-backed data.
- [ ] No fix-session controls appear in v2 report pages.
- [ ] No v2 path reads usage from OpenCode DB fallback.
- [ ] Human review before production host rollout.

### Phase 4: Rollout, Docs, And Production Proof

#### Task 12: Update Operations, README, And Usage Documentation

**Description:** Document how to configure, run, verify, and operate CR v2, including production host deployment, SQLite location, audit/report directories, llm-proxy config, recent/progress pages, callback retry, and rollback.

**Acceptance criteria:**
- [ ] `README.md` or a dedicated docs page explains local run, daemon CLI run modes, required env vars, generated `opencode.json`, SQLite/audit/report directories, and test commands.
- [ ] `docs/spec-cr-v2-cloudflare-reuse.md` and `docs/design-cr-v2-cloudflare-reuse.md` are updated if implementation choices differ from plan.
- [ ] Documentation records [comain/unit-test-agent](https://github.com/comain/unit-test-agent) reuse decisions and any rebuild exceptions with source path, target path, and reason.
- [ ] Ops configs/scripts mention SQLite/audit/report directories and no hacioded secrets.
- [ ] Non-Ticket release evidence remains N/A; if Ticket is later assigned, add release approval evidence and `release-approval command` check before `/ship`.

**Verification:**
- [ ] Documentation links point to real files.
- [ ] Commands in docs are executable or explicitly marked environment-specific.

**Dependencies:** Tasks 2 through 11 as docs stabilize.

**Files likely touched:**
- `README.md`
- `docs/spec-cr-v2-cloudflare-reuse.md`
- `docs/design-cr-v2-cloudflare-reuse.md`
- `ops/*`
- `scripts/*`

**Estimated scope:** S.

#### Task 13: Stage Node2 Rollout And Production Verification

**Description:** Drain v1 work, deploy v2, run synthetic verification, monitor proof signals, and define rollback.

**Acceptance criteria:**
- [ ] New triggers are frozen or drained before deploy.
- [ ] Existing JSON queued/running tasks are completed or failed with callback if they exceed the drain window.
- [ ] `runtime/tasks/*.json` is archived before switching.
- [ ] Deploy by git pull and supervisor restart on production host for both HTTP service and CR daemon.
- [ ] `/health`, `/reports/recent.html?hours=24&limit=20`, task status, progress, report detail, callback history, daemon CLI `status`, and terminal dashboard are verified.
- [ ] One synthetic light CR trigger produces required reviewer session ID, token usage, report, and callback.
- [ ] Production proof query confirms every `success/passed` production task has required reviewer runs, session IDs, token totals, private context artifacts, and judge acceptance; `skipped` tasks have ignored-file-only proof.
- [ ] Rollback command is documented and tested as previous commit + supervisor restart for both service and daemon.

**Verification:**
- [ ] `python -m pytest` before deploy.
- [ ] Node2 smoke commands recorded in deployment notes.
- [ ] Recent page and report pages load after supervisor restart.
- [ ] Daemon CLI status shows heartbeat and no stuck claimed tasks after the synthetic run.

**Dependencies:** Tasks 1 through 12.

**Files likely touched:**
- `ops/supervisor/*.conf`
- `scripts/start_service.sh`
- `scripts/run_service.sh`
- deployment notes in docs

**Estimated scope:** M.

## Requirement Coverage

| Source | Requirement / design decision | Covered by task(s) | Notes |
| --- | --- | --- | --- |
| spec | Use SQLite for tasks, reviewer runs, findings, events, usage, dashboard | 3, 6, 9 | |
| spec | CI/CI/manual triggers enqueue tasks; daemon claims queued work like [comain/unit-test-agent](https://github.com/comain/unit-test-agent) | 3, 6, 8 | |
| spec | Daemon CLI with terminal dashboard and operational parameters | 8, 12, 13 | |
| spec | Copy/adapt [comain/unit-test-agent](https://github.com/comain/unit-test-agent) modules directly; no shared package | 4, 6, 8, 12 | Reuse-first is mandatory for source-map behavior; rebuilds need documented exceptions. |
| spec | Use LangGraph workflow | 6, 7 | |
| spec | Reuse [comain/unit-test-agent](https://github.com/comain/unit-test-agent) OpenCode config/model routing | 2, 4 | |
| spec | Deterministic context preparation and prompt templates | 5 | |
| spec | Replace v1 path directly; no JSON/index compatibility for new tasks | 1, 11 | Legacy JSON-backed compatibility is accepted as not fixed. |
| spec | Reuse [comain/unit-test-agent](https://github.com/comain/unit-test-agent) dashboard/recent/progress patterns | 9 | |
| spec | Deterministic guards prevent missing output pass | 5, 6, 7 | |
| spec | Raw audit artifacts private, public reports sanitized | 5, 9 | |
| spec | No zero-review success for production diffs; ignored-only is skipped | 5, 6 | |
| spec | Every reviewer run stores session ID, raw log, usage, status, duration, error | 3, 4, 6, 7 | |
| spec | No prompt-path/OpenCode DB fallback session discovery | 4, 11 | |
| spec | Stable `finding_id` and event-based operations | 3, 7, 10 | |
| spec | Token usage includes cache read/write and single total display | 3, 4, 9, 10 | |
| spec | Recent/progress explain where time was spent | 8, 9 | |
| design | Coarse LangGraph nodes and typed `ReviewState` | 6, 7 | |
| design | Risk thresholds and specialist selection | 5 | |
| design | Judge precision/recall/dedupe/accepted-empty behavior | 7 | |
| design | Provider-chain config, llm-proxy base URL, no `apiKey`, snapshots | 2, 4 | |
| design | Generated `opencode.json` backup/restore/cleanup/exclusion | 4, 5 | |
| design | SQLite schema, indexes, reviewer attempt identity | 3 | |
| design | API route changes, `finding_id` feedback, no fix-session routes | 9, 10, 11 | |
| design | Report sessions and combined token/cost display | 9, 10 | |
| design | Feedback subsessions and all-resolved pass/ack recomputation | 10 | New auth is accepted as not fixed in Task 1. |
| design | Callback retry state machine and dashboard state | 8 | |
| design | Daemon queue claim, lease, CLI, terminal dashboard, heartbeat/stale recovery | 3, 6, 8 | |
| design | V1 code removal contract | 11 | |
| design | Capacity/performance budgets and indexes | 3, 9, 13 | |
| design | Security: private audit dir, token redaction, no hacioded secrets | 2, 5, 9, 12 | |
| design | Rollout drain, archive JSON tasks, production host smoke, rollback | 13 | |
| design review | Not-fix Critical: legacy report compatibility | 1, 11, 13 | No legacy JSON detail adapter in this version. |
| design review | Not-fix Critical: auth-gated human resolution/pass acknowledgment | 1, 10, 13 | No new auth layer in this version; risk accepted. |
| plan skill | Documentation updates and release evidence handling | 12, 13 | Non-Ticket: release evidence N/A unless Ticket is later attached. |

## Risks And Mitigations

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Legacy JSON-backed report compatibility is not fixed | Old reports may break after v2 deploy | Accepted by Task 1; keep old static files best-effort through `StaticFiles`, do not add compatibility adapter in this version. |
| Human resolution without a new auth layer can unblock releases | Bad release can pass CI | Accepted by Task 1 for current internal tool scope; record actor/rationale where provided and keep report URL/internal network boundary. |
| Daemon queue claim bug can double-run a task | Duplicate reviewer runs/callbacks | Atomic SQLite claim transaction, lease predicates, unique reviewer attempt IDs, and multi-daemon tests. |
| Copied [comain/unit-test-agent](https://github.com/comain/unit-test-agent) modules drift from source assumptions | Hidden test-generation concepts leak into CR | Task 4 tests and Task 11 grep for test-generation terminology and v1/fix references. |
| SQLite contention under concurrent reviewers/feedback | Slow recent/progress or task writes | WAL, busy timeout, short transactions, indexed queries, bounded OpenCode concurrency. |
| OpenCode stream format changes | Missing session/token data | Stream fixtures and explicit guard: missing required session/token blocks pass. |
| Generated `opencode.json` dirties reviewed repo | Review context polluted or user config overwritten | Backup/restore/delete and post-run git status assertion. |
| Public report leaks raw artifacts or secrets | Proprietary data exposure | Private audit dir, sanitized artifact tests, no mounted raw paths. |
| Removing v1 too early breaks route coverage | Deployment regression | Product-surface checkpoint before v1 retirement; fake-runner E2E before removal. |

## Open Questions

- Should non-Ticket production rollout require an internal release evidence file even without Ticket/release approval?

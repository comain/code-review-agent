# cr_agent

Chinese: [README.zh.md](README.zh.md)

V2 journey blog: [English](docs/blog-cr-v2-journey.en.md) | [中文](docs/blog-cr-v2-journey.md)

`cr_agent` is a CI code-review task service. It receives CI, manual API, or git-hook trigger requests, prepares a Git checkout, runs OpenCode reviewer sessions, validates structured review output, renders public reports, and sends structured JSON callbacks to CI.

Operational commands, daemon setup, supervisor examples, hooks, benchmark scripts, and trigger examples live in [operation.md](operation.md).

## Capabilities

- Accept review tasks from CI pages, CI triggers, external hooks, or manual API calls.
- Prepare Git workspaces and run OpenCode reviews concurrently.
- In CR v2, record OpenCode session id, model, token usage, cost, raw log path, and structured output per reviewer, judge, and feedback session.
- Validate reviewer and judge JSON, normalize findings, and generate `result.json`, `comments.json`, and `index.html`.
- Support finding feedback, human non-fix or false-positive resolution, and CI callback retry.
- Prevent a production diff from passing with zero reviewer sessions; only provable non-production-only changes can be marked `skipped`.

## Layout

- `src/cr_agent`: service code.
- `src/cr_agent/review_v2`: SQLite-backed CR v2 workflow, context, prompt, reviewer, judge, feedback, and dashboard code.
- `src/cr_agent/review_v2/templates`: CR v2 prompt templates, reviewer personas, shared references, and language/file-type rules.
- `config/env`: environment defaults.
- `ops`: nginx and supervisor examples.
- `scripts`: service startup, daemon startup, mock callback, hook, benchmark, and sync scripts.
- `tests`: unit tests.
- `docs`: CR v2 spec, design, plan, roadmap, ADRs, and architecture notes.

## CR v2 Overview

CR v2 uses SQLite as the source of truth for tasks, reviewer runs, findings, feedback sessions, token usage, daemon heartbeats, and recent/progress queries. HTTP trigger routes enqueue work only; a separate daemon claims queued tasks from SQLite and runs the read-only review workflow.

Trigger endpoints require `CR_AGENT_TRIGGER_TOKEN`. Calls to `/api/v1/tasks/trigger`, `/api/v1/hooks/trigger`, and `/api/v1/ci/trigger` must provide the token through `Authorization: Bearer <token>`, `X-CR-Agent-Token`, or `X-Webhook-Token`. If the token is not configured, trigger intake is disabled.

When `CR_AGENT_REVIEW_V2_ENABLED=true`, trigger, recent, status, detail, and feedback surfaces use the v2 SQLite path. Legacy index-based feedback and fix-session routes are rejected.

## Review Triage

CR v2 performs deterministic triage before calling an LLM. The LLM does not decide which files are in review scope.

`ContextBuilder` creates two context sets:

- Review context: `diff.patch`, `changed_files.json`, and `changed_lines.json`. These include only reviewable production files and are injected into reviewer prompts.
- Audit context: `diff_full.patch`, `all_changed_files.json`, `all_changed_lines.json`, `file_summaries.json`, `matched_review_rules.json`, `feature_spec_references.json`, `coverage_plan.json`, and `llm_context.json`. These preserve the full diff, all files, skip reasons, rule matches, feature references, and checksums in the private audit directory.

File classification:

- `production`: files that are not tests, ignored docs, bloat, or unsupported files. Any production diff requires at least one required reviewer.
- `test`: common test directories and test filename patterns. Test files are not injected into reviewer prompts.
- `ignored`: README, Markdown docs, and documentation under `docs/`.
- `bloat/unsupported`: lockfiles, minified assets, source maps, snapshots, images, archives, binaries, fonts, database dumps, `*.jar`, `*.class`, and files outside the supported extension allowlist.

Risk tiers are computed from production file count, production changed-line count, filtered diff size, truncation state, and path triggers:

- `skipped`: no production diff. The result is marked `gate_status=skipped`, not reviewed-pass.
- `light`: small production diff with no specialist trigger. Runs required `correctness_light`.
- `standard`: default production review path. Runs required `correctness` and up to two optional specialists by priority.
- `full`: large, truncated, or sensitive production diff. Runs required `correctness` and all specialist reviewers.

Specialist triggers inspect production file paths:

- `security`: token, secret, password, auth, permission, API key, and similar security-sensitive paths.
- `api_contract`: API modules, RPC provider APIs, route/model/schema/report-template contracts, and stable interface paths.
- `config_release`: config, env, deploy, supervisor, callback, ack, and release paths.
- `performance`: scheduler, worker, concurrency, DB/query, usage, cost, and performance paths.

## Workflow Semantics

CR v2 uses a queue-backed workflow:

1. `prepare_repo`: prepare checkout and resolve review diff range.
2. `prepare_context`: write private audit artifacts, filter test/ignored/bloat/unsupported files, and compute changed-line and rule metadata.
3. `rank_risk`: compute `skipped`, `light`, `standard`, or `full`.
4. `plan_reviewers`: persist the reviewer plan into SQLite.
5. `run_reviewers`: render reviewer prompts and run independent OpenCode reviewer sessions in parallel.
6. `judge_findings`: run a final `cr_judge` OpenCode session over successful reviewer outputs.
7. `normalize_findings`: enforce schema, changed-line anchors, scope, severity, de-duplication, and reviewer attribution.
8. `finalize_task`: persist findings, aggregate token/cost, render public report artifacts, send CI callback, and mark the task `passed`, `failed`, or `skipped`.

Key invariants:

- Public reports contain sanitized results only. Prompts, raw diffs, raw JSONL logs, secrets, and audit artifacts stay private.
- SQLite is the live state source. Public `result.json` and `index.html` are derived snapshots.
- `gate_status=passed` cannot come from zero reviewer sessions. Zero-review tasks must be `skipped` with proof.
- `cr_judge` is a final LLM session. Local normalization only enforces hard constraints and never promotes unsupported candidates.
- Feedback and human resolution recompute task pass status when all findings are resolved.

## Report And Progress Surfaces

- `/task-status/{task_id}`: lightweight status page with stage, review mode, report link, progress link, and CR v2 documentation link. It does not show findings.
- `/reports/{task_id}/progress`: parent task progress page with reviewer plan, reviewer sessions, final judge session, status, session id, model, token/cost, duration, and feedback sessions.
- `/reports/{task_id}/feedback-sessions/{feedback_session_id}/progress`: single-finding feedback progress page.
- `/reports/{task_id}/index.html`: final report page with summary, combined token/cost, severity-grouped findings, blocking labels, review sessions, and feedback sessions.
- `/reports/recent.html?hours=24&limit=200`: recent task dashboard.

Token/cost aggregation includes reviewer sessions, the `cr_judge` session, and feedback sessions. Cache-read/cache-write tokens are persisted in SQLite; report summaries show a compact combined cost.

## Prompt Construction

CR v2 prompts are built from `src/cr_agent/review_v2/templates/reviewer.md.j2`, `judge.md.j2`, and `PromptRenderer`. Long ad hoc prompt strings should not be assembled inside runners.

Each reviewer prompt includes:

- JSON-only output contract.
- Reviewer profile overlay from `reviewer_profiles.py`.
- Prompt reference files: `references/review.md`, persona file, `static-analysis-checklist.md`, `false_positive_patterns.md`, `review-practices.md`, and deterministic `references/rules/*.md` matches.
- Bounded feature spec/reference files discovered from tracked repo files.
- CI request metadata, filtered changed files, filtered changed lines, and filtered inline diff.

Rules:

- Findings must be anchored to changed lines from the inline diff.
- Reviewers may inspect unchanged related code, dependencies, or call chains to validate impact and reduce false positives.
- Test/ignored/bloat/unsupported files are not injected into prompts.
- Prompt reference files are extensible assets; add personas, references, or file-type rules through templates and deterministic path-rule matching.
- The judge consumes successful reviewer structured outputs and context metadata, then balances recall, precision, de-duplication, and severity.

## Reviewer Axes

All reviewer axes share the same outer prompt and JSON contract. The axis-specific behavior comes from `src/cr_agent/review_v2/reviewer_profiles.py`.

| Axis | Persona | When it runs | Focus |
| --- | --- | --- | --- |
| `correctness_light` | `code-reviewer.md` | `light` risk | Small-diff correctness and obvious high-signal issues. |
| `correctness` | `code-reviewer.md` | Required for non-skipped standard/full reviews | Correctness, state transitions, edge cases, retries, idempotency, concurrency, and maintainability issues that can cause bugs. |
| `security` | `security-auditor.md` | Security trigger or `full` mode | Trust boundaries, validation, injection, sensitive data, logging, callbacks, and unsafe model/tool output handling. |
| `api_contract` | `system-design-reviewer.md` | API/schema/RPC/report/template/callback paths or `full` mode | Compatibility, provider/consumer ownership, migrations, rollback, public report/callback contracts. |
| `config_release` | `test-engineer.md` | Config/deploy/supervisor/callback/release paths or `full` mode | Defaults, overrides, deployment files, rollback, smoke checks, retries, stale leases, and timeouts. |
| `performance` | `web-performance-auditor.md` | Scheduler/worker/concurrency/DB/query/usage/perf paths or `full` mode | Unbounded loops, pagination, N+1, heavy request-thread work, token/cost/runtime, cache, batching, and backpressure. |

## References

- [operation.md](operation.md): run, deploy, daemon, supervisor, hook, benchmark, CI integration, and verification commands.
- [docs/cr_v2_roadmap.md](docs/cr_v2_roadmap.md): CR v2 roadmap and design tradeoffs.
- [docs/spec-cr-v2-cloudflare-reuse.md](docs/spec-cr-v2-cloudflare-reuse.md): CR v2 spec.
- [docs/design-cr-v2-cloudflare-reuse.md](docs/design-cr-v2-cloudflare-reuse.md): CR v2 design.
- [docs/plan-cr-v2-cloudflare-reuse.md](docs/plan-cr-v2-cloudflare-reuse.md): CR v2 implementation plan.

## License

[MIT](LICENSE)

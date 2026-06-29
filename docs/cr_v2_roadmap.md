# CR Agent V2 Roadmap

## Status

Draft. This records the current investigation and preferred direction for the next review architecture.

Detailed Phase 1 spec for the next step is tracked in
[`docs/spec-cr-v2-cloudflare-reuse.md`](spec-cr-v2-cloudflare-reuse.md).

## Context

`cr_agent` currently works as a FastAPI task service:

- receives CI/manual trigger
- prepares a git workspace
- runs one `opencode run` analysis prompt
- parses a single `AnalysisResult`
- writes report artifacts
- supports per-finding discussion, missed-issue feedback, and fix sessions

Recent production issues exposed limits in this model:

- reports can show misleading empty state such as "0 files investigated" or "no issues found"
- one large OpenCode session makes failure cause hard to isolate
- review comments are stored by array index, which is fragile once findings are deduped, merged, reordered, or produced by multiple reviewers
- token usage and latency are hard to explain without per-reviewer session records
- the current report is a final JSON snapshot, while users need operations on each finding: discuss, fix, mark false positive, downgrade, resolve

## External References Reviewed

### Alibaba Open Code Review

Alibaba's open-code-review implementation uses a strongly deterministic front half:

- git refs are validated before review
- diff is parsed into per-file `Diff` records
- file filtering is deterministic through default excludes, extension allowlist, user include/exclude, and binary/deleted checks
- path-based rules are resolved before the LLM prompt
- each file runs as a bounded subtask with concurrency and timeout controls
- plan phase is threshold-gated
- tool calls and token budgets are capped
- line numbers are resolved deterministically first, with LLM relocation only as fallback

Important limitation found in code: the current implementation is mostly per-file review. The README mentions "smart file bundling", but the inspected code dispatches one subtask per file. Cross-file bugs depend on the model noticing related files and calling tools such as `file_read_diff`.

Useful takeaways:

- deterministic coverage accounting is valuable
- per-file/per-unit session traces make debugging easier
- line anchoring should be service-owned where possible
- rule matching and file filtering should not be left entirely to prompts
- prompt rules, file filtering, and language/file-type rule overlays are reusable without adopting the full per-file execution model

But we do not want to copy the per-file-only split directly because it can miss cross-file contract/config/caller bugs and repeats context across sessions.

Reusable Alibaba patterns for CR v2:

- **Review prompt rule library**: keep the Cloudflare-style reviewer fanout, but add Alibaba-style rule references as deterministic prompt inputs. The useful shape is a default rule plus ordered path-specific rules for Java, Kotlin, TypeScript/JavaScript, C/C++, Rust, config files, mapper/DAO XML, package manifests, build files, YAML, JSON, and properties. CR v2 should store the matched rule names in `prompt_inputs.json` and private audit metadata so a finding can be traced back to the rule set that guided the reviewer.
- **Path-rule matching before prompt rendering**: resolve path-specific rule overlays before each reviewer prompt. This is more stable than telling the model "consider language-specific rules" in natural language. The implementation should use the same precedence idea: system defaults first, optional project rules next, explicit task/config rules highest. A project rule can either replace the system rule or merge with it.
- **File skip and allowlist policy**: expand the current CR v2 test/bloat filtering into a configurable policy with supported extension allowlist, built-in default excludes, project include/exclude globs, binary/deleted checks, and explicit skip reasons. Keep the current non-production skip proof in reports. `exclude` should win over `include`; `include` should allow a project to force-review files normally skipped by built-in defaults when the repo owner knows they are meaningful.
- **Language/file-type reviewer overlays**: do not add one reviewer process per language by default. Instead, attach language and file-type rule overlays to the selected Cloudflare-style reviewer. For example, `correctness_light` reviewing Java should receive Java-specific NPE, switch fall-through, thread-safety, and performance checks; reviewing mapper XML should receive SQL/parameter/closing-tag checks; reviewing package/build files should receive dependency and build compatibility checks.
- **Previewable coverage plan**: add a preview artifact that lists all changed files, skipped files, eligible production files, matched rule overlays, and selected reviewers before any LLM call. This directly addresses "0 files investigated" and lets users/debuggers see whether triage is wrong before model inference starts.
- **External relocation as fallback**: keep service-owned changed-line anchoring as the primary source of truth, but consider Alibaba's separate relocation step only when the model returns an imprecise line. Relocation should never make an unchanged-file issue eligible; it should only repair an anchor for an already accepted changed-line finding.

Patterns to avoid or adapt carefully:

- Do not split every file into an isolated review session by default. Use deterministic bundles only when a change is too large for one reviewer prompt or when files are naturally paired, such as i18n resource files or generated schema pairs.
- Do not let file-level language rules override specialist selection. A Dubbo provider API change should still trigger `api_contract`; a security-sensitive Java file should still trigger `security`; language rules should sharpen the reviewer, not replace risk classification.
- Do not copy Alibaba's lower recall tradeoff wholesale. CR v2 should use Alibaba-style deterministic constraints to reduce noise, while keeping Cloudflare-style specialist reviewers and judge/coordinator flow for cross-file and cross-domain bugs.

### Uber UReview

Uber's UReview article points toward deterministic pipeline orchestration:

- ingestion and preprocessing determine eligible files and structured context
- specialized assistants review different issue classes
- post-processing filters grade quality, dedupe, and suppress noisy categories
- feedback loops tune prompts, thresholds, and category policy

Useful takeaways:

- "one-shot prompt" is not enough for production review
- architecture and post-processing are as important as prompt wording
- review quality improves when findings pass a quality/filtering stage

### Cloudflare AI Code Review

Cloudflare's approach is the preferred direction for `cr_agent` v2.

Observed architecture from public writeups:

- GitLab CI component runs OpenCode
- a coordinator classifies merge requests into `trivial`, `lite`, or `full`
- specialized reviewers run for code quality, security, codex compliance, documentation, performance, and release impact
- a runtime plugin exposes a `spawn_reviewers` tool that launches child OpenCode sessions through the OpenCode SDK
- model selection is centrally controlled per reviewer
- results are grouped by category and severity
- prior review rounds are considered so fixed findings are acknowledged rather than repeated
- output is posted back as structured MR comments
- token usage, model routing, failover, and cache-read accounting are first-class operational concerns

Useful takeaways:

- keep OpenCode as the execution substrate
- run bounded specialist OpenCode sessions instead of one giant prompt
- store every child session ID
- use risk tiering to avoid full fanout for every task
- use a coordinator/judge pass for dedupe, severity normalization, and false-positive suppression
- isolate partial failures: one failed reviewer should not silently become a passing empty report

## Preferred Direction

Adopt a Cloudflare-style orchestration model, but keep enough deterministic service-level control to make coverage and failures auditable.

Recommended v2 flow:

```text
TaskService
  -> prepare repo
  -> collect diff and changed-file metadata
  -> deterministic file filtering and risk classification
  -> build shared context files
  -> run selected OpenCode reviewer sessions in parallel
  -> persist reviewer runs and raw outputs
  -> coordinator/judge pass
  -> persist normalized findings as first-class records
  -> render report and callback
```

Reviewer fanout should be selected by risk tier:

```text
trivial
  -> skip or lightweight correctness review

lite
  -> correctness/code_quality
  -> targeted specialist when sensitive files are touched

full
  -> correctness/code_quality
  -> security
  -> performance
  -> API/contract
  -> config/release
  -> coordinator/judge
```

## Storage Model

Findings should become first-class records with stable IDs. The final report JSON should be a derived snapshot, not the source of truth.

Proposed files for the current JSON-file storage model:

```text
runtime/reports/<task_id>/
  result.json                  # derived snapshot for report/callback compatibility
  findings.json                # source of truth for current finding records
  finding_events.jsonl         # append-only discussion/status/fix history
  reviewer_runs.json           # child OpenCode sessions and usage
  context/
    diff.patch
    changed_files.json
    risk.json
    reviewer_plan.json
  reviewers/
    <reviewer>/output.json
    <reviewer>/stdout.log
    <reviewer>/stderr.log
  fix_sessions/
    <fix_session_id>.json
```

Proposed finding record:

```json
{
  "finding_id": "f_...",
  "task_id": "...",
  "source_reviewer": "security",
  "source_review_run_id": "...",
  "opencode_session_id": "...",
  "status": "open",
  "resolution": null,
  "severity": "high",
  "category": "security",
  "confidence": 0.86,
  "title": "...",
  "body": "...",
  "impact": "...",
  "recommendation": "...",
  "file_path": "src/foo.py",
  "start_line": 42,
  "end_line": 48,
  "line_anchor": {
    "commit": "...",
    "existing_code": "...",
    "existing_code_hash": "sha256:...",
    "diff_hunk": "@@ ..."
  },
  "suggestion_code": "...",
  "dedupe_key": "sha256:..."
}
```

Proposed event model:

```json
{
  "event_id": "...",
  "finding_id": "f_...",
  "task_id": "...",
  "type": "user_comment",
  "actor": "user",
  "body": "...",
  "metadata": {},
  "created_at": "..."
}
```

Event types should cover:

- `finding_created`
- `user_comment`
- `model_reply`
- `severity_changed`
- `marked_false_positive`
- `reopened`
- `fix_session_started`
- `fix_session_completed`
- `status_changed`

This supports discuss/fix/false-positive operations without losing history.

## Current Comment Handling

Current implementation:

- original comments are `record.result.findings`
- each finding is identified by array index
- per-finding discussion is stored as `FindingFeedbackThread(finding_index=...)`
- general missed-issue feedback and fix sessions share the same `feedback.json`
- discussion triggers an async model re-review
- re-review can keep the finding open, downgrade severity, or resolve it as false positive
- downgrades and false-positive confirmations are appended to `/opt/app/issues/*.accepted-finding-feedback.jsonl`
- after feedback, the effective report is recalculated and report/callback may be updated

This is useful but fragile. V2 should migrate from index-based threads to stable finding IDs and append-only events.

## Reviewer Run Records

Every OpenCode child session should be persisted explicitly:

```json
{
  "review_run_id": "...",
  "task_id": "...",
  "reviewer": "security",
  "opencode_session_id": "...",
  "status": "success",
  "started_at": "...",
  "finished_at": "...",
  "duration_seconds": 91,
  "usage": {
    "input_tokens": 120000,
    "output_tokens": 8000,
    "cache_read_tokens": 90000,
    "cache_write_tokens": 12000,
    "total_tokens": 128000,
    "cost_usd": 0.18
  },
  "findings_count": 3,
  "raw_output_path": "runtime/reports/<task_id>/reviewers/security/output.json",
  "error": null
}
```

This removes the need to rediscover session IDs from prompts or paths.

## Coverage and Failure Semantics

V2 reports should explicitly show:

- files changed
- files eligible
- files skipped and reasons
- reviewers planned
- reviewers completed
- reviewers failed
- findings by reviewer/category/severity
- whether final result is `success`, `completed_with_warnings`, or `failed`

An empty finding list should only be trusted when coverage is non-empty and required reviewers completed. Otherwise report should say "review incomplete" rather than "no issues found".

## Implementation Plan

### Phase 1: Data Model and Observability

- add `reviewer_runs.json`
- store OpenCode session ID for every analysis/fix/feedback run
- add stable `finding_id` while keeping index compatibility in existing report JSON
- add token/cache/cost breakdown by run and reviewer
- show coverage and reviewer status on recent/reports pages

### Phase 2: Split One Analysis into Reviewer Runs

- build shared context files under report directory
- add deterministic risk classifier
- run a small fixed reviewer set first: `correctness`, `security`, `config_release`
- parse each reviewer output into normalized findings
- persist raw reviewer output and usage

### Phase 3: Coordinator/Judge

- dedupe findings across reviewers
- suppress low-confidence findings
- normalize severity/category
- validate findings map to changed code or explain why unchanged context is relevant
- produce final `result.json` for current report/callback compatibility

### Phase 4: Finding Lifecycle

- migrate per-finding discussion to `finding_events.jsonl`
- make false-positive, downgrade, reopen, and fix actions event-driven
- make fix sessions reference `finding_id` instead of index
- keep old index endpoints temporarily by resolving index to `finding_id`

### Phase 5: Quality Feedback Loop

- sync accepted false-positive patterns, accepted downgrade patterns, and missed-issue patterns into reviewer-specific guidance
- add dashboard metrics for false-positive rate, accepted feedback rate, missed issue confirmations, reviewer latency, and cost

## Open Questions

- Should orchestration live purely in `cr_agent` Python, or should we expose an OpenCode plugin/tool similar to Cloudflare's `spawn_reviewers`?
- Should reviewer fanout run through separate `opencode run` processes or one long-lived `opencode serve` instance?
- What minimum risk tier should trigger security review?
- Should coordinator use the strongest model while specialist reviewers use cheaper models?
- Should we persist to SQLite/Postgres now, or keep JSON files until the model stabilizes?

## Current Recommendation

Start with service-level orchestration in `cr_agent`, not a custom OpenCode plugin. The service already owns task records, reports, feedback, callbacks, usage, and deployment. Keeping orchestration outside OpenCode makes failure recovery and report state easier to reason about.

Use OpenCode as the execution backend for specialist reviewer sessions. Add an OpenCode plugin later only if we need a richer coordinator transcript or lower session startup overhead.

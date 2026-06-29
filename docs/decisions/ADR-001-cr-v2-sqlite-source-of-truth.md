# ADR-001: Use SQLite As CR V2 Source Of Truth

## Status

Accepted

## Date

2026-06-24

## Context

CR v1 stores task state in JSON files and reconstructs token usage from OpenCode DB paths. V2 needs durable reviewer runs, finding IDs, finding events, token/cost records, heartbeats, dashboard queries, and progress pages. JSON files are too fragile for concurrent workers and indexed recent-job queries.

## Decision

Use a CR-specific SQLite database as the source of truth for tasks, reviewer runs, findings, finding events, fix sessions, task events, runner heartbeats, token usage, and dashboard data.

Generated report files remain derived artifacts.

## Alternatives Considered

### Keep JSON Task Files

Pros: minimal migration.

Cons: weak concurrency, no indexed queries, hard event append semantics, difficult dashboard aggregation.

Rejected because v2 needs first-class task/reviewer/finding operations.

### Reuse [comain/unit-test-agent](https://github.com/comain/unit-test-agent) TaskDB Schema Directly

Pros: fastest copy.

Cons: [comain/unit-test-agent](https://github.com/comain/unit-test-agent) schema is test-generation specific, with class/coverage/mutation terminology.

Rejected because CR needs CR-specific table names and fields.

### External Database

Pros: stronger multi-host scaling.

Cons: extra deployment and ops cost for a single production host service.

Rejected for this version because production host SQLite with WAL is sufficient.

## Consequences

- New v2 tasks are not readable by the previous JSON task store.
- Rollback ignores v2 SQLite data.
- Dashboard/recent/progress queries become efficient and auditable.
- Implementation must keep transactions short and store large logs/prompts as files.

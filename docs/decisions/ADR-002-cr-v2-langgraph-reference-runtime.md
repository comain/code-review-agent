# ADR-002: Use LangGraph And Copy/Adapt comain/unit-test-agent Runtime Modules

## Status

Accepted

## Date

2026-06-24

## Context

CR v1 uses one long OpenCode prompt through a shell command template. [comain/unit-test-agent](https://github.com/comain/unit-test-agent) already has language-neutral modules for LangGraph state/workflow patterns, OpenCode process spawning, JSONL stream parsing, session/token capture, model routing, task events, and dashboard/progress concepts.

## Decision

Use LangGraph for the CR v2 workflow and copy/adapt [comain/unit-test-agent](https://github.com/comain/unit-test-agent) runtime modules into `src/cr_agent/review_v2`.

The copied code must be renamed around CR concepts and must not expose [comain/unit-test-agent](https://github.com/comain/unit-test-agent) test-generation terminology in CR schemas or APIs.

## Alternatives Considered

### Keep One-Shot Runner

Pros: smallest diff.

Cons: keeps opaque failure modes, no per-reviewer sessions, no deterministic guard stages.

Rejected because it does not solve the root production issues.

### Extract Shared Internal Package

Pros: less duplication long term.

Cons: creates cross-repo release sequencing and versioning before the interface is stable.

Rejected for this version. Copy/adapt first, extract later if both repos converge.

### Custom Local Workflow Runner

Pros: fewer dependencies.

Cons: rebuilds graph/state/transition semantics that LangGraph already provides.

Rejected because the spec explicitly requires LangGraph and [comain/unit-test-agent](https://github.com/comain/unit-test-agent) has proven the pattern.

## Consequences

- `pyproject.toml` adds a LangGraph dependency.
- CR v2 can reuse [comain/unit-test-agent](https://github.com/comain/unit-test-agent) execution behavior while keeping CR-specific data models.
- Future [comain/unit-test-agent](https://github.com/comain/unit-test-agent) fixes may need manual cherry-pick until a shared package exists.

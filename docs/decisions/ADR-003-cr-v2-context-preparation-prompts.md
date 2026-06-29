# ADR-003: Make LLM Context Preparation A Deterministic Stage

## Status

Accepted

## Date

2026-06-24

## Context

The old one-shot review path built review context inside `GitClient.collect_review_context` and `OpencodeRunner._build_prompt`. This coupled diff construction, CI metadata, guideline text, and OpenCode execution. It also made it hard to prove what a reviewer actually saw.

V2 needs deterministic diff artifacts, normalized CI/CI request context, prompt inputs per reviewer, and package-owned prompt assets.

## Decision

Create a dedicated `review_v2.context` stage that writes shared context artifacts before any OpenCode reviewer starts, and a `review_v2.prompts` stage that renders reviewer prompts from templates.

The prompt uses `src/cr_agent/review_v2/templates/` as the single prompt asset root. That package directory contains Jinja prompt templates and Markdown prompt reference files under `references/`, including `references/review.md`, reviewer personas, and adapted review references. Each reviewer run stores exact `prompt_inputs.json` plus final `prompt.md`.

## Alternatives Considered

### Keep Context In Python Prompt Strings

Pros: minimal movement.

Cons: hard to test, hard to audit, mixes execution with prompt construction.

Rejected because auditability is a core v2 requirement.

### Split Guidelines Under A Separate Skill Directory

Pros: separates generic review guidelines from Python package code.

Cons: prompt behavior is split across repository roots, package installs can drift from local skill files, and tests cannot fully prove the deployed prompt source.

Rejected. Review prompt assets are vendored into package data.

## Consequences

- Context prep and prompt rendering can be unit and snapshot tested.
- Every reviewer run has reproducible prompt inputs.
- OpenCode can read raw context files when inline summaries are not enough.

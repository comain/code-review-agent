# ADR-004: Store Raw Review Audit Artifacts Outside The Public Report Tree

## Status

Accepted

## Date

2026-06-24

## Context

CR v2 needs raw audit artifacts for debugging and reproducibility: full diffs, changed-line metadata, CI request context, prompt inputs, final prompts, raw OpenCode JSONL, and reviewer output JSON. The current FastAPI app mounts `settings.report_dir` as public static files under `/reports`. Storing raw artifacts there would expose proprietary code and prompts to every report viewer.

## Decision

Store raw audit artifacts under a private `settings.audit_dir`, defaulting to `runtime/audit`, and store only sanitized derived artifacts under `settings.report_dir`.

Public report artifacts include `index.html`, sanitized `result.json`, and sanitized detail JSON needed by the report UI. They must not contain raw diff, raw prompt inputs, raw OpenCode logs, provider config, or secrets.

## Alternatives Considered

### Keep Raw Artifacts Under Report Directory

Pros: easy links and inspection.

Cons: exposes sensitive source diffs and prompts through the existing static mount.

Rejected because it widens the data exposure of every report URL.

### Add Authentication Around Entire `/reports`

Pros: protects all report files.

Cons: changes current report access behavior and CI/report links.

Rejected for this version. We keep public report behavior and move only raw artifacts private.

## Consequences

- Debugging raw reviewer runs requires filesystem access or a future explicit admin/debug endpoint.
- Report rendering must copy only sanitized data into `report_dir`.
- Tests must prove private artifacts are not under the mounted report root.

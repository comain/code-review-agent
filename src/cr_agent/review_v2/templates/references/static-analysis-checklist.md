# Static Analysis Checklist


Use this checklist for production or production-bound reviews where the change touches callbacks, replay protection, async processing, locks, retries, idempotency, state transitions, configuration-driven limits, batch jobs, downstream write APIs, SQL, security-sensitive logic, or performance-sensitive paths.

## Review Focus

1. Confirm the requirement is completely covered, including boundary cases.
2. Inspect thread pools, futures, async callbacks, and scheduled work for blocking, leaks, starvation, races, and missing timeouts.
3. Check lock usage: acquire/release symmetry, unlock only after successful acquire, and token/lease validation before unlock.
4. Verify timeout, retry, circuit-breaker, fallback, and exception handling semantics match the side effects.
5. Require idempotency keys and deduplication for write APIs, message consumers, callbacks, and notifications.
6. Verify state transitions allow only legal moves and protect terminal states from rollback.
7. Check batch processing for stable ordering, bounded fetches, retry backoff, and starvation avoidance.
8. Check critical configuration for hard caps, safe defaults, and alerts on dangerous values.
9. Inspect SQL injection, authorization bypass, sensitive-data exposure, and hot-path performance risks.

## Frequent Incremental-Review Misses

### Callback And Notification Replay

- Signature checks without a timestamp window still allow replay.
- Missing `eventId`, `nonce`, request id, or Redis/database dedupe allows retries and malicious replay to trigger duplicate side effects.
- Require timestamp-window validation plus a durable or cache-backed idempotency key when the callback writes state or calls downstream systems.

### Terminal-State Protection

- Branching on current state is weaker than defining and enforcing legal transitions.
- Terminal states must not be rolled back to processing, valid, unverified, or similar mutable states.
- Prefer `where status in (...)`, version-based optimistic locking, or equivalent CAS protection around terminal transitions.

### Configuration Maximums

- Limits, inventory caps, discounts, user quotas, and similar values must not rely only on external config.
- The risk is often financial or operational loss from misconfiguration rather than a normal code error.
- Require hard caps, safe defaults, and alerts when configured values exceed expected ranges.

### Batch Fetches Without Stable Ordering

- `select ... limit N` without `order by` can keep retrying the same historical bad rows.
- Bad rows can occupy the first page forever and starve new work.
- Require stable ordering such as `id` or `create_time`, plus failure backoff or dead-letter handling.

### Retry Semantics Without Idempotency

- Retrying a downstream write without `request_id`, `out_request_no`, or an equivalent idempotency key can duplicate side effects.
- Partial success plus continued retry is especially risky when the downstream system is not idempotent.
- Link the retry decision to an explicit idempotency guarantee.

### Validation And Calculation Unit Drift

- Validation may use percentages while calculation uses per-mille, basis points, cents, grams, or another unit.
- Variable names, comments, storage units, and multiply/divide factors can disagree.
- Inspect conversions such as `* 10`, `* 100`, `/ 100`, currency scaling, and unit-specific constants before accepting the change.

### Distributed Lock Misuse

- Unlocking unconditionally in `finally` after failing to acquire the lock can release another worker's lock.
- Unlocking by key only, without token or lease validation, can corrupt concurrent ownership.
- Treat these as high-risk because they usually reproduce only under concurrency.

### Multiple Entrypoints Triggering One Side Effect

- Controllers, scheduled jobs, and message consumers often share the same core handler.
- If all entrypoints can trigger the same write without an idempotency key, lock, or status CAS, duplicate persistence, duplicate downstream calls, or state overwrite can occur.
- Review the shared side-effect boundary, not just the changed caller.

## False-Positive Controls

### Configuration Field Absence

- A field missing from local properties does not prove the feature is broken.
- Check injection through custom config annotations, `@Value`, `@ConfigurationProperties`, external config centers, environment variables, and runtime defaults.
- When the source is external or uncertain, phrase the finding as "needs confirmation" and explain the risk window.

### Similar Structure, Different Business Rule

- Similar sync, import, or scheduled classes may intentionally differ when they process different entities or lifecycle rules.
- Treat differences as likely defects only when the same entity, rule, and entrypoint class should behave consistently.
- Downgrade uncertain business-rule differences to confirmation questions.

### Third-Party Response Contracts

- "Return a richer error" is not always correct for third-party callbacks.
- If both sides contract only `success`/`fail`, do not require custom error codes without evidence.
- Phrase the recommendation as conditional when the external contract is unknown.

### Scheduler Framework Semantics

- Do not assume a scheduled job needs an extra distributed lock.
- First confirm whether the scheduler framework already guarantees shard exclusivity, single execution, or failover behavior.
- Recommend extra locking only when framework semantics do not cover the risk.

## Reporting Guidance

- State a definite defect only when code evidence is decisive.
- Use "needs confirmation", "risk window", or "verify" when the conclusion depends on external config, third-party contracts, or framework behavior.
- Avoid overstating high-false-positive scenarios as guaranteed failures.

## Finding Severity

- `fatal`: direct financial loss, data corruption, severe security vulnerability, or broken concurrency control.
- `high`: core flow interruption, large-scale misclassification, or retries that amplify side effects.
- `medium`: functional boundary defect, observability gap, or meaningful regression risk.
- `low`: maintainability issue, code smell, or mild performance issue.
- `info`: non-blocking improvement note.

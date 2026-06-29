# Review Practices Reference


Use this reference when the main review checklist is not enough.

## Multi-Model Review Pattern

Use different review perspectives for different risks:

```text
Model A writes the code
  -> Model B reviews correctness and architecture
  -> Specialist pass reviews security, verification, or performance
  -> Human or workflow makes the final call
```

In CR v2, this maps to deterministic reviewer planning plus dedicated reviewer profiles.

## Dead Code Hygiene

After refactoring or implementation changes:

1. Identify code that is now unreachable or unused.
2. List it explicitly.
3. Report it only when the dead code is introduced or made misleading by the current change.

Do not turn unrelated cleanup into a blocking finding.

## Handling Disagreements

Apply this hierarchy:

1. Technical facts and data override opinions and preferences.
2. Style guides are the authority on style matters.
3. Software design should be evaluated on engineering principles.
4. Codebase consistency is acceptable if it does not degrade health.

Do not accept "I'll clean it up later" unless it is a genuine emergency and the follow-up is tracked.

## Honesty In Review

- Do not rubber-stamp.
- Do not soften real production bugs.
- Quantify problems when possible.
- Push back on approaches with clear problems.
- Defer gracefully when the author has decisive context.

## Dependency Discipline

Before adding any dependency:

1. Does the existing stack solve this?
2. How large is the dependency?
3. Is it actively maintained?
4. Does it have known vulnerabilities?
5. Is the license compatible?

Prefer the standard library and existing utilities over new dependencies.

## Common Rationalizations

| Rationalization | Reality |
|---|---|
| "It works, that's good enough" | Working code can still be unreadable, insecure, or architecturally wrong. |
| "I wrote it, so I know it's correct" | Authors are blind to their own assumptions. |
| "We'll clean it up later" | Later rarely comes; require cleanup before merge when practical. |
| "AI-generated code is probably fine" | AI code needs more scrutiny, not less. |
| "The tests pass, so it's good" | Tests are necessary but not sufficient. |

## Red Flags

- Reviews that only check whether tests pass.
- Security-sensitive changes without security-focused review.
- Large changes that are too big to review properly.
- Bug fixes without regression tests.
- Review findings without severity labels.
- Deferred cleanup without tracking.

## Final Verification

- All Critical issues are resolved.
- All Important issues are resolved or explicitly deferred with justification.
- Tests pass.
- Build succeeds.
- The verification story documents what changed and how it was verified.

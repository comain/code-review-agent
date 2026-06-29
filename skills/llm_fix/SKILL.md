---
name: llm-fix
description: Use when the user wants to repair findings from a cr_agent report through multi-round conversation, controlled code changes, self-review, and merge request generation.
---

# LLM Fix

Use this skill for report-driven repair sessions. The workflow is:

1. Confirm issue scope and repair boundaries with the user.
2. Confirm a per-finding repair plan with the user.
3. Modify code only after explicit user confirmation.
4. Run a second-pass self-review; if not passed, continue fixing until passed or the retry cap is reached.
5. Push a dedicated fix branch and create an MR back to the user's branch.
6. Wait for the user's final confirmation; if the user is not satisfied, restart from scope confirmation.

Read [references/fix_workflow.md](references/fix_workflow.md) before producing or reviewing fixes.

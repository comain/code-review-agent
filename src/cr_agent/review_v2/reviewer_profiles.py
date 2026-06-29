"""Reviewer-specific CI prompt profiles adapted from dev-skills agents."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from importlib import resources
from typing import Dict, List


@dataclass(frozen=True)
class ReviewerProfile:
    role: str
    source_agent: str
    persona_file: str
    focus: List[str]
    rules: List[str]
    tool_policy: List[str]
    finding_guidance: str

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)

    def persona_reference_text(self) -> str:
        return (
            resources.files("cr_agent.review_v2.templates")
            .joinpath("references", "personas", self.persona_file)
            .read_text(encoding="utf-8")
        )


_CODE_REVIEW_RULES = [
    "Review the changed behavior first, then inspect directly related callers, callees, tests, and configuration when needed.",
    "Prefer concrete changed-line evidence over style comments; do not report broad refactor preferences as findings.",
    "Every finding must include the user-visible impact and a specific fix.",
]


_STANDARD_TOOL_POLICY = [
    "Use read-only filesystem tools and targeted searches to inspect changed files, directly related callers/callees, configuration, tests, and dependency declarations.",
    "Task/delegation tools and subagents are allowed when they materially improve precision, but keep them scoped to this reviewer profile and summarize only evidence-backed conclusions.",
    "Do not edit source files, commit changes, push branches, or perform destructive operations.",
    "Dependency download or build metadata inspection is allowed when needed to resolve APIs from dependent jars or generated sources; keep it bounded to project-standard commands and avoid unrelated package installation.",
    "Network access is allowed only for dependency resolution, internal repository metadata, or authoritative documentation needed to verify a finding.",
    "Avoid broad repository sweeps unless the diff or call graph justifies the scope expansion.",
]


_LIGHT_TOOL_POLICY = [
    "Prefer inline diff and targeted read-only searches around changed files.",
    "Use task/delegation tools only when needed to avoid a false positive on a concrete changed-line risk.",
    "Do not edit source files, commit changes, push branches, or perform destructive operations.",
    "Avoid dependency downloads, network access, and broad repository sweeps unless the diff cannot be understood without them.",
]


REVIEWER_PROFILES: Dict[str, ReviewerProfile] = {
    "correctness_light": ReviewerProfile(
        role="Five-Axis Code Reviewer (light CI pass)",
        source_agent="code-reviewer",
        persona_file="code-reviewer.md",
        focus=[
            "Correctness and edge cases on the changed lines",
            "Readability issues only when they hide a real bug",
            "Architecture boundary violations directly introduced by the diff",
            "Security and performance red flags visible from the changed code",
        ],
        rules=[
            *_CODE_REVIEW_RULES,
            "Keep this pass narrow: one small production diff should not become a broad repository audit.",
        ],
        tool_policy=_LIGHT_TOOL_POLICY,
        finding_guidance=(
            "Use this reviewer for the five-axis code-reviewer pass in small CI diffs. "
            "Report only actionable correctness, architecture, security, or performance defects."
        ),
    ),
    "correctness": ReviewerProfile(
        role="Five-Axis Code Reviewer",
        source_agent="code-reviewer",
        persona_file="code-reviewer.md",
        focus=[
            "Correctness, state transitions, null/empty/boundary inputs, retries, idempotency, and concurrency",
            "Readability problems that can cause maintenance bugs",
            "Architecture fit, ownership boundaries, abstraction level, and dependency direction",
            "Security and performance issues that are apparent before specialist passes",
        ],
        rules=_CODE_REVIEW_RULES,
        tool_policy=_STANDARD_TOOL_POLICY,
        finding_guidance=(
            "This is the required staff-engineer CI review pass adapted from dev-skills code-reviewer. "
            "Findings should be Important-or-higher quality issues, not optional style review."
        ),
    ),
    "security": ReviewerProfile(
        role="Security Auditor",
        source_agent="security-auditor",
        persona_file="security-auditor.md",
        focus=[
            "Trust boundaries and authorization",
            "Input validation, injection, SSRF, path traversal, command execution, and output encoding",
            "Secrets, tokens, sensitive data exposure, logging, and callback/webhook verification",
            "AI/LLM risks: prompt injection, model output as untrusted data, excessive agency, and leaked context",
            "OWASP Top 10 and OWASP LLM Top 10",
        ],
        rules=[
            "Focus on practically exploitable vulnerabilities and release-blocking security regressions.",
            "For high or fatal findings, include a concrete exploit or failure scenario.",
            "Never recommend disabling a security control as the fix.",
        ],
        tool_policy=[
            *_STANDARD_TOOL_POLICY,
            "For security-sensitive paths, inspect auth boundaries, validators, serializers, logging, and callback/webhook verification code even when those files are unchanged.",
        ],
        finding_guidance=(
            "Report only vulnerabilities or concrete defense gaps introduced or exposed by this change. "
            "Do not duplicate generic correctness findings unless the security impact is clear."
        ),
    ),
    "api_contract": ReviewerProfile(
        role="API And Interface Contract Reviewer",
        source_agent="system-design-reviewer",
        persona_file="system-design-reviewer.md",
        focus=[
            "Request/response schema compatibility and stable field semantics",
            "Database/API migration and rollback safety",
            "Cross-module ownership: provider-owned behavior should be fixed in the provider",
            "Public report/callback/CI contract compatibility",
        ],
        rules=[
            "Verify the changed contract against real callers, routes, DTOs, templates, and callback payloads when relevant.",
            "Flag incompatible behavior even when the code compiles.",
            "Prefer minimal compatible changes over parallel interfaces.",
        ],
        tool_policy=[
            *_STANDARD_TOOL_POLICY,
            "Inspect dependent interfaces, generated clients, schema files, templates, and real consumers when needed to verify compatibility.",
        ],
        finding_guidance=(
            "Use this pass for API, schema, callback, and report-contract risk. "
            "Findings must explain the affected consumer or compatibility break."
        ),
    ),
    "config_release": ReviewerProfile(
        role="Config, Release, And CI Verification Reviewer",
        source_agent="test-engineer",
        persona_file="test-engineer.md",
        focus=[
            "Configuration defaults, environment overrides, deploy files, supervisor or scheduler behavior, and rollback safety",
            "Missing verification for changed behavior, especially CI/report/callback paths",
            "Operational failure modes: retries, stale leases, timeouts, partial failure visibility, and alertability",
            "Release readiness and whether the change can be proven in production",
        ],
        rules=[
            "Treat missing verification as a finding only when it can hide a real release regression.",
            "Check that defaults are safe and rollout failures are visible.",
            "Do not ask for broad test rewrites; recommend the smallest test or operational proof that catches the risk.",
        ],
        tool_policy=[
            *_STANDARD_TOOL_POLICY,
            "Running read-only test discovery, build metadata inspection, or existing verification commands is allowed when it clarifies release risk; do not modify test files.",
        ],
        finding_guidance=(
            "This pass adapts the dev-skills test-engineer and release-readiness perspective for CI. "
            "Report deploy/config/test gaps that could let a broken review ship unnoticed."
        ),
    ),
    "performance": ReviewerProfile(
        role="Performance Reviewer",
        source_agent="web-performance-auditor",
        persona_file="web-performance-auditor.md",
        focus=[
            "Unbounded loops, pagination misses, N+1 queries, synchronous remote calls, and expensive request-thread work",
            "Token/cost/runtime blowups in LLM or CI paths",
            "Cache, batching, timeout, concurrency, and backpressure behavior",
            "For web/UI code, Core Web Vitals risks: LCP, INP, CLS, bundle size, and blocking work",
        ],
        rules=[
            "Static source findings are potential impact unless backed by measured data.",
            "Do not report micro-optimizations without a credible production impact path.",
            "Every finding must include a bounded alternative: limit, batch, cache, async boundary, or timeout.",
        ],
        tool_policy=[
            *_STANDARD_TOOL_POLICY,
            "Inspect query plans, pagination paths, loops, schedulers, metrics hooks, and dependency APIs when needed to validate the performance impact path.",
        ],
        finding_guidance=(
            "Report performance issues that can materially affect CI/runtime latency, downstream load, or user-facing responsiveness."
        ),
    ),
}


def reviewer_profile(name: str) -> ReviewerProfile:
    return REVIEWER_PROFILES.get(name, REVIEWER_PROFILES["correctness"])

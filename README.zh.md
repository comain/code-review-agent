# cr_agent

English: [README.md](README.md)

V2 journey blog: [中文](docs/blog-cr-v2-journey.md) | [English](docs/blog-cr-v2-journey.en.md)

CIpre-release阶段 CR 任务承载服务。服务接收 CI/manual/git hook 触发请求，准备 Git 工作区，调用 OpenCode reviewer session，校验结构化结果，生成公开报告，并以结构化 JSON 回调 CI。

运行、部署、daemon、supervisor、hook、压测和示例请求见 [operation.md](operation.md)。

## 能力

- 接收CI页面、CI trigger、外部 hook 或手动 API 的触发请求。
- 并发执行 Git 仓库准备与 `opencode` 审查。
- 在 CR v2 中按 reviewer/judge/feedback session 记录 OpenCode session id、model、token、cost、raw log 和结构化输出。
- 校验 reviewer/judge JSON，归一化 finding，生成 `result.json`、`comments.json`、`index.html`。
- 支持 finding feedback、human non-fix/false-positive resolution、CI callback retry。
- 防止生产 diff 零 reviewer 通过；只有可证明的 non-production-only change 才能 `skipped`。

## 目录

- `src/cr_agent`: 服务代码。
- `src/cr_agent/review_v2`: SQLite-backed CR v2 workflow、context、prompt、reviewer、judge、feedback、dashboard 代码。
- `src/cr_agent/review_v2/templates`: CR v2 prompt 模板、reviewer persona、通用参考材料和语言/文件类型规则。
- `config/env`: 环境变量配置。
- `ops`: nginx、supervisor 示例配置。
- `scripts`: 启动、daemon、mock callback、hook、压测和同步脚本。
- `tests`: 单元测试。
- `docs`: CR v2 spec/design/plan/roadmap/ADR。

## CR v2 概览

CR v2 使用 SQLite 作为任务、reviewer run、finding、feedback session、token usage、daemon heartbeat 和 recent/progress 查询的来源。HTTP 触发只入队；独立 daemon 从 SQLite claim 任务并执行只读 review workflow。

Trigger 接口要求配置 `CR_AGENT_TRIGGER_TOKEN`。`/api/v1/tasks/trigger`、`/api/v1/hooks/trigger`、`/api/v1/ci/trigger` 必须通过 `Authorization: Bearer <token>`、`X-CR-Agent-Token` 或 `X-Webhook-Token` 提供共享密钥；未配置 token 时 trigger intake 会被禁用。

开启 `CR_AGENT_REVIEW_V2_ENABLED=true` 后，触发、recent/status/detail/feedback 使用 v2 SQLite 路径。旧的 index-based feedback 与 fix-session 路由会被拒绝。

## Review Triage

CR v2 在调用 LLM 前先做确定性 triage。LLM 不负责决定哪些文件进入审查范围。

`ContextBuilder` 会生成两套上下文：

- 审查上下文：`diff.patch`、`changed_files.json`、`changed_lines.json`，只包含可审查的生产文件，会进入 reviewer prompt。
- 审计上下文：`diff_full.patch`、`all_changed_files.json`、`all_changed_lines.json`、`file_summaries.json`、`matched_review_rules.json`、`feature_spec_references.json`、`coverage_plan.json`、`llm_context.json`，保留完整 diff、所有文件、过滤原因、rule match、可匹配的原始 feature spec/reference 和 checksum，只放在私有 audit 目录。

文件分类规则：

- `production`: 非 test、非 ignored、非 bloat/unsupported 的文件。只要存在 production diff，就必须至少运行一个 required reviewer。
- `test`: `test/`、`tests/`、`__tests__/`、`testdata/`、`*.test.*`、`*.spec.*`、`*_test.*`、Java/Kotlin/Scala/Groovy 的 `*Test`/`*Tests`/`*IT`/`*ITCase` 文件。test 文件不会进入 reviewer prompt。
- `ignored`: `README.md`、Markdown 文档和 `docs/` 下的文档。
- `bloat/unsupported`: lockfile、minified asset、source map、snapshot、图片/音视频、压缩包、二进制、字体、数据库 dump、`*.jar`/`*.class`，以及不在 supported extension/file allowlist 内的文件。

risk tier 由生产文件数、生产 changed-line 数、过滤后的 `diff.patch` 大小、context 截断状态和路径触发器决定：

- `skipped`: 没有 changed file，或所有 changed file 都是 test/ignored/bloat/unsupported/non-production。结果标记为 `gate_status=skipped`，不渲染成“已审查通过”。
- `light`: 生产文件不超过 5 个、生产 changed lines 不超过 120、过滤后的 diff 不超过 80 KiB，且没有 specialist trigger。只运行 required `correctness_light`。
- `standard`: 默认生产审查路径。运行 required `correctness`，并按优先级最多附加两个 optional specialists。
- `full`: context 截断、生产文件数超过 30、生产 changed lines 超过 800、过滤后的 diff 超过 500 KiB，或触发 security/api_contract/config_release/performance 轴。运行 required `correctness` 和全部 CI specialist reviewers。

specialist trigger 只看生产文件路径：

- `security`: token、secret、password、auth、permission、api_key 等安全敏感路径。
- `api_contract`: `*/api/*`、`*-api/` module、Dubbo/RPC provider API、`remote`/`facade` interface、routes、models、schemas、report template 等稳定接口或展示契约路径。
- `config_release`: config、env、deploy、supervisor、callback、ack 等发布/回调路径。
- `performance`: scheduler、worker、concurrency、db、query、usage 等并发、查询、成本或性能路径。

## Workflow Semantics

当前 v2 workflow 的业务语义：

1. `prepare_repo`: 准备仓库 checkout，解析 review diff range。
2. `prepare_context`: 写私有 audit artifact，过滤 test/ignored/bloat/unsupported 文件，计算 changed-line map 和 matched review rules。
3. `rank_risk`: 按确定性规则计算 `skipped`、`light`、`standard` 或 `full`。
4. `plan_reviewers`: 写 `reviewer_plan.json`，并把 reviewer plan 持久化到 SQLite。除 `skipped` 外，生产 diff 必须有 required reviewer。
5. `run_reviewers`: 对每个 planned reviewer 构造 prompt，按 `CR_AGENT_REVIEW_V2_REVIEWER_CONCURRENCY` 并行启动独立 OpenCode reviewer session，记录 session id、model、raw log、结构化输出、token 和 cost。required reviewer 失败会阻断最终通过；optional reviewer 失败会被记录但不进入 judge。
6. `judge_findings`: 启动独立 `cr_judge` OpenCode session，使用 `judge.md.j2` prompt 汇总成功 reviewer 输出，判断哪些 candidate 可进入最终报告，并输出 `accepted_findings`/`rejected_candidates` JSON。
7. `normalize_findings`: 对 `cr_judge` 输出再做确定性 guard，包括 JSON schema、changed-line anchor、文件范围、severity、去重和 reviewer attribution 校验。最终 finding 保留原始贡献 reviewer/run id，便于后续定向复审。
8. `finalize_task`: 写 finding、聚合 token/cost、生成公开 report/result、发送 CI callback，并把任务置为 `passed`、`failed` 或 `skipped`。

关键不变量：

- 公开 report 只包含 sanitized result，不暴露 prompt、raw diff、raw JSONL、token secret 或 audit artifact。
- SQLite 是实时状态来源；公开 `result.json`/`index.html` 是派生快照，`/reports/{task_id}/detail`、`/reports/{task_id}/progress/data` 和 recent 页面直接从 SQLite 汇总。
- `gate_status=passed` 不能是零 reviewer 成功；只有 `gate_status=skipped` 可以没有 reviewer，并且必须有 non-production-only 证明。
- `cr_judge` 是 final LLM session，不是纯本地筛选；本地 `JudgeNormalizer` 只负责硬约束兜底，不提升无证据 candidate。
- feedback/human resolution 更新 finding 状态后，会重新计算任务是否已全部 resolved；全部 resolved 时任务可重新标记为 `passed` 并触发幂等 callback。

## Report And Progress Surfaces

CR v2 面向用户的页面和 JSON 均来自 SQLite：

- `/task-status/{task_id}`: 轻量状态页，展示当前阶段、review mode、报告链接、进度链接和 [CR v2 Overview](README.md#cr-v2-overview) 文档入口，不展示 finding 列表。
- `/reports/{task_id}/progress`: 父任务进度页，展示 reviewer plan、并行 reviewer session、最终 `cr_judge` session、每个 session 的 status/session id/model/token/cost/duration，以及 feedback session 列表。
- `/reports/{task_id}/feedback-sessions/{feedback_session_id}/progress`: 单个 finding feedback 复审进度页，展示原始 finding、原始贡献 reviewer/session、feedback OpenCode session、token/cost 和事件流。
- `/reports/{task_id}/index.html`: 最终报告页，展示报告信息、摘要、合并 token/cost、按 severity 分组的 finding 列表、blocking/non-blocking 标识、review session 表和 feedback session 表。
- `/reports/recent.html?hours=24&limit=200`: recent task dashboard，展示最近 CR v2/legacy 任务、finding 数、分层 severity、combined cost/token 和任务入口。

token/cost 聚合规则：

- reviewer session、`cr_judge` session 和 feedback session 都计入任务总 token/cost。
- 表格中 session 级 token/cost 用于定位慢任务或异常模型；摘要中的 cost 是合计值。
- cache-read/cache-write tokens 保存在 SQLite，并在详情/progress 摘要中展示，cost 只展示合计金额。

## Feedback Re-review

CR v2 是只读 review agent，不提供 fix-session 或自动改代码。报告页的 finding 操作只做 feedback/re-review 或 human non-fix 标记：

- 用户提交 finding feedback 后，服务先创建 `feedback_sessions` 行并立即返回，页面可以跳转到 feedback progress；后台线程继续执行 OpenCode 复审。
- feedback prompt 是一个独立、收敛的复审 session，会带上原始 finding、原始 OpenCode session id、原始贡献 reviewer profile 和用户反馈。
- feedback 只复审该 finding 是否仍应保持 open，不重跑整个 workflow，也不重新启动其他 reviewer。
- finding 保存 `reviewer_run_id`，feedback session 保存 `parent_reviewer_run_id`；因此复审可以定向到原始贡献 reviewer，而不是重新 fanout 全部 reviewer。
- feedback session 返回 resolved 后，finding 会变为 `resolved_model_false_positive` 或 `re_reviewed_pass`；用户人工标记则为 `human_non_fix`。
- 当所有 finding 都被 feedback 或 human non-fix 解决后，任务重新变为 `gate_status=passed`，并触发幂等 CI callback。

## Prompt Construction

CR v2 prompt 统一由 `src/cr_agent/review_v2/templates/reviewer.md.j2`、`judge.md.j2` 和 `PromptRenderer` 构造。不要在 runner 里拼接临时长字符串。

每个 reviewer prompt 固定包含：

- JSON-only 输出契约：`summary`、`pass_check`、`score`、`findings[]`。
- reviewer profile overlay：来自 `reviewer_profiles.py`，定义该 reviewer 的 focus、allowed tools、tool policy 和 persona 文件。
- prompt reference files：`references/review.md`、对应 persona、`static-analysis-checklist.md`、`false_positive_patterns.md`、`review-practices.md`，以及按 changed file 确定性匹配的 `references/rules/*.md` 语言/文件类型规则。
- feature spec/reference files：`ContextBuilder` 会从 tracked 文件中按 ticket key（如 `DEMO-123`、`DEMO-7271`）、changed spec-like 文件和 `spec/design/requirement/prd/proposal/story/task/release-approval/需求/方案/设计` 等路径关键词，提取最多 5 个 bounded reference 到 `feature_spec_references.json`。这用于让 reviewer/judge 对照原始需求或设计审查实现。
- CI request、过滤后的 `changed_files.json`、过滤后的 `changed_lines.json`。
- 过滤后的 inline diff。inline diff 上限为 `min(CR_AGENT_REVIEW_V2_PROMPT_MAX_BYTES, 500000)`，且至少 20000 bytes；被截断时追加 `CR_V2_INLINE_DIFF_TRUNCATED` 标记。

prompt 规则：

- reviewer 只能报告由 inline diff changed line 直接支撑的问题；可以检查未改代码、依赖或调用链来验证影响和降低误报，但 finding anchor 必须落在 changed line 上。
- reviewer 和 `cr_judge` 输出的用户可见自然语言必须使用中文，包括 `summary`、finding `title`、`detail`、`suggestion` 和空结果 rationale；JSON key、文件路径、代码标识符、异常名、SQL/index 名等技术术语保持原样。
- feature spec/reference 可以证明 feature intent，但不能单独成为 finding 证据；只有当 spec 写出具体要求且 changed code 明确违背或遗漏时，才能报告 spec mismatch。
- reviewer 可按 profile 使用工具、下载项目标准依赖、查看相关未改代码或调用 task/delegation 工具；探索必须围绕 changed file、相关 symbol、直接调用方/被调用方或 profile 明确允许的范围。
- test/ignored/bloat/unsupported 文件不进入 prompt。它们只出现在私有 audit metadata 中，供排查为什么任务被跳过或为什么某个文件未审查。
- prompt reference 文件是可扩展资产；新增 persona、参考材料或语言/文件类型规则应通过模板 reference 和 path-rule 匹配统一接入，不在业务 runner 中硬编码。
- judge prompt 只消费成功 reviewer 的结构化输出、risk/context metadata 和必要的 changed-line map；它负责 precision/recall 取舍，本地 normalizer 负责硬约束。
- feedback prompt 独立存放在 audit feedback 目录，引用原始 finding 和原始 reviewer profile；它不使用完整 reviewer fanout prompt。

## Reviewer Axis Prompts

所有 reviewer axis 共享同一个外层 prompt 契约：

- 入口模板：`src/cr_agent/review_v2/templates/reviewer.md.j2`。
- 输出契约：只返回一个 JSON object，包含 `summary`、`pass_check`、`score`、`findings[]`。
- 证据边界：finding 必须锚定 inline diff 的 changed line；可以读取未改代码、调用链、配置、依赖声明或依赖源码来验证影响和降低误报。
- 工具边界：OpenCode per-turn `opencode.json` 允许只读探索和 bounded tool/subagent use，禁止 edit/write/patch；不同 axis 通过 profile 进一步收窄探索目标。
- 参考材料：每个 prompt 注入通用 review 指南、persona、static-analysis checklist、false-positive patterns、review practices、可匹配的原始 feature spec/reference，以及按文件类型匹配的 `references/rules/*.md`。

不同 axis 的差异来自 `src/cr_agent/review_v2/reviewer_profiles.py`：

| Axis | Persona | 什么时候运行 | Prompt focus | Finding 要求 |
| --- | --- | --- | --- | --- |
| `correctness_light` | `code-reviewer.md` | `light` 风险，小生产 diff 且无 specialist trigger | changed-line correctness、边界输入、可见的架构/安全/性能红旗；避免扩展成全仓审计 | 只报小 diff 中可直接证明的 actionable bug，不报风格和宽泛重构建议 |
| `correctness` | `code-reviewer.md` | 所有非 skipped 的标准/完整生产审查 required pass | 正确性、状态流转、null/empty/boundary、重试、幂等、并发、维护性会导致 bug 的可读性问题、架构边界 | Important-or-higher 质量问题；必须说明用户影响和具体修复 |
| `security` | `security-auditor.md` | 安全敏感路径触发，或 `full` 模式 | 授权/信任边界、输入校验、注入、SSRF、路径穿越、命令执行、输出编码、secret/token/sensitive data、日志、callback/webhook、LLM 风险 | 只报实际可利用或具体防御缺口；high/fatal 必须有 exploit/failure scenario；不要建议关闭安全控制 |
| `api_contract` | `system-design-reviewer.md` | API/schema/RPC/report/template/callback/稳定接口路径触发，或 `full` 模式 | 请求/响应兼容性、字段语义、DB/API migration、rollback、provider/consumer ownership、公共 report/callback/CI contract | 必须指出受影响 consumer 或兼容性破坏；编译通过但运行期契约不兼容也要报 |
| `config_release` | `test-engineer.md` | config/env/deploy/supervisor/callback/ack/发布路径触发，或 `full` 模式 | 配置默认值、环境覆盖、部署文件、supervisor、scheduler、rollback、最小验证、重试、stale lease、timeout、可观测性 | 只在缺失验证会隐藏真实发布回归时报告；建议最小 test、smoke 或运维证明 |
| `performance` | `web-performance-auditor.md` | scheduler/worker/concurrency/db/query/usage/cost/perf 路径触发，或 `full` 模式 | 无界循环、分页缺失、N+1、同步远程调用、request-thread 重活、LLM/CI token/cost/runtime、cache/batch/timeout/backpressure | 不报微优化；每个 finding 必须给 bounded 替代方案，如 limit、batch、cache、async boundary 或 timeout |

`cr_judge` 是单独的 final judge prompt：

- 模板：`src/cr_agent/review_v2/templates/judge.md.j2`。
- 输入：成功 reviewer 的 JSON 输出、risk tier/reasons、changed files/lines、CI request、可匹配的原始 feature spec/reference、context paths。
- 逻辑：先做 recall 收集所有 candidate，再按 schema、scope、evidence、precision、confidence、dedupe、severity、gate 顺序筛选。
- 输出：`accepted_findings[]`、`rejected_candidates[]`、必要时 `accepted_empty_rationale`。
- 语言：accepted finding 的标题、详情和建议必须中文化；如果 reviewer candidate 是英文，`cr_judge` 负责改写为中文后再进入最终报告。
- provenance：保留 `source_review_run_id` 和 `source_reviewer`，后续 finding feedback 可只复审原始贡献 reviewer。

## 参考文档

- [operation.md](operation.md): 启动、部署、daemon、supervisor、hook、压测、CI 接入和验证命令。
- [docs/cr_v2_roadmap.md](docs/cr_v2_roadmap.md): CR v2 roadmap 和外部方案取舍。
- [docs/spec-cr-v2-cloudflare-reuse.md](docs/spec-cr-v2-cloudflare-reuse.md): CR v2 spec。
- [docs/design-cr-v2-cloudflare-reuse.md](docs/design-cr-v2-cloudflare-reuse.md): CR v2 design。
- [docs/plan-cr-v2-cloudflare-reuse.md](docs/plan-cr-v2-cloudflare-reuse.md): CR v2 implementation plan。

## License

[MIT](LICENSE)

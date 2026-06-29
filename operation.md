# cr_agent Operation

运行、部署、daemon、supervisor、hook、压测和 CI 接入 runbook。

架构、review triage、workflow semantics 和 prompt construction 见 [README.md](README.md)。

## 本地启动

```bash
python3.9 -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
python -m cr_agent.main
```

## 一键启动

```bash
./scripts/start_service.sh
```

默认会自动执行：

- `git pull --ff-only`
- `python -m pip install -e '.[dev]'`
- 加载 `config/env/common.env` 和 `config/env/<env>.env`
- 前台启动 `python -m cr_agent.main`

默认环境：

- `prod`

可选环境：

- `dev`
- `test`
- `prod`

例如：

```bash
./scripts/start_service.sh
./scripts/start_service.sh test
./scripts/start_service.sh prod
```

如果只想在当前已安装环境里启动，不做 `git pull` 和 `pip install`：

```bash
./scripts/run_service.sh
```

前提是当前 shell 已经加载过对应环境变量。

## 环境配置

环境变量文件位于：

- `config/env/common.env`
- `config/env/dev.env`
- `config/env/test.env`
- `config/env/prod.env`

推荐做法：

- 通用默认值放 `common.env`
- 环境差异放 `dev.env/test.env/prod.env`
- 机器相关值，例如真实 `CR_AGENT_REPORT_BASE_URL`、`CR_AGENT_CI_ACK_URL`，按环境文件分别维护

## 关键配置

通过环境变量覆盖，变量名前缀为 `CR_AGENT_`。默认由 `config/env/*.env` 提供。

- `CR_AGENT_REPORT_BASE_URL`: 报告访问地址前缀，例如 `https://reviews.example.com/cr-agent/reports`
- `CR_AGENT_REPORT_PUBLIC_ROOT`: FastAPI 挂载静态报告的 URL 前缀，默认 `/reports`
- `CR_AGENT_OPENCODE_COMMAND_TEMPLATE`: v1 `opencode` 调用模板
- `CR_AGENT_WORKER_THREADS`: v1 并发 worker 数
- `CR_AGENT_TASK_TIMEOUT_SECONDS`: 单任务大模型调用超时
- `CR_AGENT_CALLBACK_RETRY_TIMES`: CI 回调重试次数
- `CR_AGENT_TRIGGER_TOKEN`: trigger 共享密钥。必须配置后 `/api/v1/tasks/trigger`、`/api/v1/hooks/trigger`、`/api/v1/ci/trigger` 才会接受任务；请求需携带 `Authorization: Bearer <token>`、`X-CR-Agent-Token` 或 `X-Webhook-Token`。

## CR v2 配置

CR v2 关键环境变量：

- `CR_AGENT_REVIEW_V2_ENABLED`: 是否启用 v2 路径，默认 `false`
- `CR_AGENT_REVIEW_V2_DB_PATH`: SQLite 文件，默认 `runtime/review_v2.sqlite3`
- `CR_AGENT_REVIEW_V2_AUDIT_DIR`: 私有审计目录，默认 `runtime/review_v2_audit`
- `CR_AGENT_REPORT_DIR`: 公开 report 目录，默认 `runtime/reports`
- `CR_AGENT_REVIEW_V2_DAEMON_ID`: daemon 标识，默认 hostname + pid
- `CR_AGENT_REVIEW_V2_DAEMON_POLL_INTERVAL_SECONDS`: daemon 轮询间隔
- `CR_AGENT_REVIEW_V2_DAEMON_CLAIM_LIMIT`: callback retry/claim 上限
- `CR_AGENT_REVIEW_V2_DAEMON_LEASE_SECONDS`: task lease 秒数
- `CR_AGENT_REVIEW_V2_LLM_PROXY_BASE_URL`: 默认 `http://openai-compatible.example.com/v1`
- `CR_AGENT_REVIEW_V2_OPENCODE_PROVIDER_CHAIN`: 默认 `llm-proxy:gpt-5.5`

本地 v2 启动：

```bash
source .venv/bin/activate
set -a
source config/env/common.env
source config/env/dev.env
set +a
export CR_AGENT_REVIEW_V2_ENABLED=true
python -m cr_agent.main
```

daemon 操作：

```bash
./scripts/start_cr_daemon.sh dev --foreground
python -m cr_agent.review_v2.cli once --task-db runtime/review_v2.sqlite3
python -m cr_agent.review_v2.cli status --task-db runtime/review_v2.sqlite3
python -m cr_agent.review_v2.cli dashboard --once --task-db runtime/review_v2.sqlite3
python -m cr_agent.review_v2.cli recover-stale --task-db runtime/review_v2.sqlite3
python -m cr_agent.review_v2.cli retry-callbacks --task-db runtime/review_v2.sqlite3
```

任务控制：

```bash
# 请求正在运行的任务协作停止；OpenCode 子进程会被终止，任务进入 cancelled/stopped
python -m cr_agent.review_v2.cli stop <task_id> --reason "operator stop" --task-db runtime/review_v2.sqlite3

# 立即取消任务，并把 queued/running reviewer run 标记为 cancelled
python -m cr_agent.review_v2.cli cancel <task_id> --reason "operator cancel" --task-db runtime/review_v2.sqlite3

# 清理 lease/control state，把任务放回 queued 由 daemon 重新 claim
python -m cr_agent.review_v2.cli requeue <task_id> --reason "operator retry" --task-db runtime/review_v2.sqlite3
```

HTTP admin 入口：

```bash
curl -X POST 'http://127.0.0.1:8000/api/v1/admin/tasks/<task_id>/stop?reason=operator%20stop'
curl -X POST 'http://127.0.0.1:8000/api/v1/admin/tasks/<task_id>/cancel?reason=operator%20cancel'
curl -X POST 'http://127.0.0.1:8000/api/v1/admin/tasks/<task_id>/requeue?reason=operator%20retry'
```

生成的 per-reviewer `opencode.json` 写入仓库下 `.cr_agent/opencode/configs/<turn>/opencode.json`。该临时目录会通过 symlink 暴露真实仓库内容，OpenCode 进程以该目录为 cwd 启动，不传 `--model` 或 `--dir`，从本地 `opencode.json` 读取 model/provider 配置。执行结束后会删除该临时配置目录，不会改写仓库根目录已有的 `opencode.json`。

## Mock 回调服务

脚本启动：

```bash
./scripts/run_mock_callback.sh
```

直接启动：

```bash
python scripts/mock_callback_server.py --host 0.0.0.0 --port 9999
```

默认会：

- 在终端打印收到的回调内容
- 追加写入 `runtime/mock_callback_requests.jsonl`

支持以下环境变量：

- `MOCK_CALLBACK_HOST`
- `MOCK_CALLBACK_PORT`
- `MOCK_CALLBACK_OUTPUT`

## 验证命令

```bash
python3 -m pytest
python3 -m compileall -q src
```

常用 v2 局部验证：

```bash
python3 -m pytest -q \
  tests/test_review_v2_context.py \
  tests/test_review_v2_prompts.py \
  tests/test_review_v2_risk_plan.py \
  tests/test_review_v2_workflow.py
```

## production host 发布与回滚

HTTP service 与 CR daemon 要同时处理。

```bash
cd /opt/app/cr_agent
git pull --ff-only
supervisorctl restart cr_agent
supervisorctl restart cr_agent_daemon
supervisorctl status cr_agent cr_agent_daemon
```

回滚：

```bash
cd /opt/app/cr_agent
git reset --hard <previous_commit>
supervisorctl restart cr_agent
supervisorctl restart cr_agent_daemon
supervisorctl status cr_agent cr_agent_daemon
```

production host 通过 deploy key 拉取目标分支时可用：

```bash
cd /opt/app/cr_agent
GIT_SSH_COMMAND="ssh -F /dev/null -i /opt/app/cr_agent/deploy_key_ed25519 -o IdentitiesOnly=yes -o PreferredAuthentications=publickey -o StrictHostKeyChecking=accept-new" \
  git pull --ff-only origin <branch>
```

## Supervisor 托管

已提供示例配置：

- `ops/supervisor/cr_agent.conf`
- `ops/supervisor/cr_agent_daemon.conf`
- `ops/supervisor/mock_callback.conf`
- `ops/supervisor/cr_agent_group.conf`

典型部署方式：

```bash
cp ops/supervisor/cr_agent.conf /etc/supervisord.d/cr_agent.conf
cp ops/supervisor/cr_agent_daemon.conf /etc/supervisord.d/cr_agent_daemon.conf
cp ops/supervisor/mock_callback.conf /etc/supervisord.d/mock_callback.conf
cp ops/supervisor/cr_agent_group.conf /etc/supervisord.d/cr_agent_group.conf
supervisorctl reread
supervisorctl update
supervisorctl status cr_agent cr_agent_daemon
```

默认 supervisor 会执行：

```bash
./scripts/start_service.sh prod
```

mock callback 的 supervisor 默认是关闭的，如需联调时启动：

```bash
supervisorctl start cr_agent_mock_callback
supervisorctl status cr_agent_mock_callback
```

如果希望把主服务和 mock 一起管理：

```bash
supervisorctl start cr_agent_stack:*
supervisorctl stop cr_agent_stack:*
```

如果部署目录、运行用户或日志目录不同，改 `directory`、`command`、`stdout_logfile`、`stderr_logfile` 即可。

推荐的生产运维命令：

```bash
supervisorctl status cr_agent
supervisorctl restart cr_agent
tail -f /opt/app/cr_agent/logs/supervisor.stdout.log
tail -f /opt/app/cr_agent/logs/supervisor.stderr.log
```

联调 mock callback 时：

```bash
tail -f /opt/app/cr_agent/logs/mock_callback.stdout.log
tail -f /opt/app/cr_agent/logs/mock_callback.stderr.log
```

推荐上线步骤：

1. 修改 `config/env/prod.env` 为目标机真实地址和参数。
2. `supervisorctl stop cr_agent`
3. `cd /opt/app/cr_agent && git pull`
4. `supervisorctl start cr_agent`
5. `supervisorctl status cr_agent`

如果不想让 supervisor 在每次启动时自动 `git pull` 和 `pip install`，可以把 `cr_agent.conf` 里的 command 改成：

```bash
/bin/bash -lc 'source config/env/common.env && source config/env/prod.env && ./scripts/run_service.sh'
```

这种模式更适合代码发布和进程托管分离的生产环境。

## 误判范式每日同步

CR v2 不再依赖 v1 的 JSONL feedback 文件作为主链路。所有 finding feedback 会记录到 SQLite：

- `findings.status`
- `finding_events`
- `feedback_sessions`

CR v2 daemon 进程内每天从 SQLite 挖掘已解决 finding。同步不会修改正在运行的源码目录；它会克隆目标分支，在隔离 worktree 中更新 reviewer prompt 使用的反馈归档，推送同步分支并创建 MR：

- 目标文件：`src/cr_agent/review_v2/templates/references/false_positive_patterns.md`
- 目标分支：默认 `init`
- 同步分支：`mr/feedback-pattern-sync-<YYYYmmdd-HHMMSS>`
- 输入状态：`resolved_model_false_positive`、`re_reviewed_pass`、`human_non_fix`
- 输出分组：误判范式、已采纳反馈范式
- 默认调度：每天 `23:38`，按 `Asia/Shanghai` 日期每天最多执行一次

可配置项：

```bash
CR_AGENT_REVIEW_V2_FEEDBACK_PATTERN_SYNC_ENABLED=true
CR_AGENT_REVIEW_V2_FEEDBACK_PATTERN_SYNC_HOUR=23
CR_AGENT_REVIEW_V2_FEEDBACK_PATTERN_SYNC_MINUTE=38
CR_AGENT_REVIEW_V2_FEEDBACK_PATTERN_SYNC_LIMIT=200
CR_AGENT_REVIEW_V2_FEEDBACK_PATTERN_SYNC_TARGET_BRANCH=init
CR_AGENT_REVIEW_V2_FEEDBACK_PATTERN_SYNC_WORK_ROOT=
```

手动触发：

```bash
curl -X POST http://127.0.0.1:8000/api/v1/admin/false-positive-sync
```

结果可在 daemon heartbeat 和 `task_events` 中观察：

- `feedback_pattern_sync_completed`
- `feedback_pattern_sync_failed`

如果 GitLab API token 未配置或 MR 创建失败，任务会保留已 push 的同步分支，并在结果中返回手工创建 MR 的 URL。配置 `CR_AGENT_GITLAB_API_TOKEN` 或 `GITLAB_TOKEN` 后可自动创建 MR。

## Git Hook 触发测试

仓库内提供了一个可直接挂到 Git 服务端 hook 的触发脚本：

- `scripts/git_hook_trigger.sh`

用途：

- 分支有新提交时自动调用 `cr_agent`
- 默认跳过 `reviewer` 的提交
- 适合挂在 `post-receive` 或 `update` hook 上做联调测试

最小环境变量：

```bash
/opt/app/cr_agent/scripts/git_hook_trigger.sh
```

可选环境变量：

- `CR_AGENT_HOOK_SKIP_USER`：默认 `reviewer`
- `CR_AGENT_HOOK_TOKEN`：如果接入侧需要额外认证可传
- `CR_AGENT_GIT_HOST`：默认 `github.com`
- `CR_AGENT_REPO_PATH`：显式指定仓库路径，例如 `comain/code-review-agent`
- `CR_AGENT_REPO_URL`：显式指定完整仓库地址
- `CR_AGENT_APP_NAME`：显式指定应用名
- `CR_AGENT_HOOK_LOG`：日志文件路径
- `CR_AGENT_FORCE_TRIGGER_USER`：手动覆盖触发用户

`post-receive` 示例：

```bash
#!/usr/bin/env bash
/opt/app/cr_agent/scripts/git_hook_trigger.sh
```

`update` 示例：

```bash
#!/usr/bin/env bash
/opt/app/cr_agent/scripts/git_hook_trigger.sh "$1" "$2" "$3"
```

## 批量压测 opencode 并发

仓库内提供了批量压测脚本：

- `scripts/opencode_benchmark_matrix.sh`

默认会对以下本地示例仓库分别跑一组并发：

- `runtime/repos/service_a`
- `runtime/repos/service_b`

默认并发组：

- `5`
- `10`

默认执行方式：

- 直接调用 `scripts/concurrency_test.py --mode opencode`
- 默认模型：`llm-proxy/gpt-5.5`

最简用法：

```bash
bash /opt/app/cr_agent/scripts/opencode_benchmark_matrix.sh
```

常用覆盖参数：

```bash
TOTAL=20 CONCURRENCY_SET=5,10,20 MODEL=llm-proxy/gpt-5.5 \
bash /opt/app/cr_agent/scripts/opencode_benchmark_matrix.sh
```

如果要给某个仓库指定真实 prompt 文件：

```bash
SERVICE_A_PROMPT_FILE=/opt/app/cr_agent/runtime/tasks/<service-a-task>.prompt.md \
SERVICE_B_PROMPT_FILE=/opt/app/cr_agent/runtime/tasks/<service-b-task>.prompt.md \
bash /opt/app/cr_agent/scripts/opencode_benchmark_matrix.sh
```

输出位置：

```bash
/opt/app/cr_agent/runtime/benchmarks/
```

## 示例请求

普通触发：

```bash
curl -X POST http://127.0.0.1:8000/api/v1/tasks/trigger \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${CR_AGENT_TRIGGER_TOKEN}" \
  -d '{
    "app_name": "demo_order_api",
    "repo_url": "git@github.com:comain/code-review-agent.git",
    "branch": "feature/demo",
    "commit_id": "abc123",
    "callback_url": "https://ci.example.com/api/task/callback",
    "trigger_source": "pipeline"
  }'
```

## CI trigger 接入

CI 可直接把 `API` 任务 URL 指向：

```text
/api/v1/ci/trigger
```

兼容 CI 透传的 `attribute.appName`、`attribute.branch`、`attribute.gitUrl`、`taskId`、`recordId`、`taskTemplateId`、`parentId`。

分析完成后，若请求里没有显式 `callback_url`，服务会自动回调 `CR_AGENT_CI_ACK_URL`，默认：

```text
http://127.0.0.1/ci/plugin/ack
```

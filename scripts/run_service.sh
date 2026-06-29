#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${PROJECT_ROOT}"
source .venv/bin/activate

echo "PROJECT_ROOT=${PROJECT_ROOT}"
echo "CR_AGENT_DEPLOY_ENV=${CR_AGENT_DEPLOY_ENV:-unknown}"
echo "CR_AGENT_BASE_DIR=${CR_AGENT_BASE_DIR:-runtime}"
echo "CR_AGENT_HOST=${CR_AGENT_HOST:-0.0.0.0}"
echo "CR_AGENT_PORT=${CR_AGENT_PORT:-8000}"
echo "CR_AGENT_REPORT_BASE_URL=${CR_AGENT_REPORT_BASE_URL:-http://127.0.0.1:8000/reports}"
echo "CR_AGENT_WORKER_THREADS=${CR_AGENT_WORKER_THREADS:-4}"
echo "CR_AGENT_REVIEW_BASE_REFS=${CR_AGENT_REVIEW_BASE_REFS:-origin/master,origin/main,origin/develop}"
echo "CR_AGENT_OPENCODE_COMMAND_TEMPLATE=${CR_AGENT_OPENCODE_COMMAND_TEMPLATE:-cat {prompt_file} | {opencode_bin} run --dir {repo_path} --format json}"

exec python -m cr_agent.main

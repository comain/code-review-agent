#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CONFIG_DIR="${PROJECT_ROOT}/config/env"
DEPLOY_ENV="${CR_AGENT_DEPLOY_ENV:-prod}"
COMMON_ENV_FILE="${CONFIG_DIR}/common.env"
ENV_FILE="${CONFIG_DIR}/${DEPLOY_ENV}.env"
FOREGROUND=0
EXTRA_ARGS=()

if [[ "${1:-}" == "dev" || "${1:-}" == "test" || "${1:-}" == "prod" ]]; then
  DEPLOY_ENV="$1"
  shift || true
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --foreground)
      FOREGROUND=1
      shift
      ;;
    *)
      EXTRA_ARGS+=("$1")
      shift
      ;;
  esac
done

cd "${PROJECT_ROOT}"
source .venv/bin/activate

set -a
source "${COMMON_ENV_FILE}"
source "${ENV_FILE}"
set +a

export CR_AGENT_DEPLOY_ENV="${DEPLOY_ENV}"
export CR_AGENT_REVIEW_V2_ENABLED="${CR_AGENT_REVIEW_V2_ENABLED:-true}"

echo "PROJECT_ROOT=${PROJECT_ROOT}"
echo "CR_AGENT_DEPLOY_ENV=${CR_AGENT_DEPLOY_ENV}"
echo "CR_AGENT_REVIEW_V2_DB_PATH=${CR_AGENT_REVIEW_V2_DB_PATH:-runtime/review_v2.sqlite3}"
echo "CR_AGENT_REVIEW_V2_AUDIT_DIR=${CR_AGENT_REVIEW_V2_AUDIT_DIR:-runtime/review_v2_audit}"

if [[ "${FOREGROUND}" == "1" ]]; then
  exec python -m cr_agent.review_v2.cli run "${EXTRA_ARGS[@]}"
fi

exec python -m cr_agent.review_v2.cli run "${EXTRA_ARGS[@]}"

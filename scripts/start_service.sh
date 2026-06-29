#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CONFIG_DIR="${PROJECT_ROOT}/config/env"
DEPLOY_ENV="${1:-${CR_AGENT_DEPLOY_ENV:-prod}}"
COMMON_ENV_FILE="${CONFIG_DIR}/common.env"
ENV_FILE="${CONFIG_DIR}/${DEPLOY_ENV}.env"

cd "${PROJECT_ROOT}"

if [[ ! -d ".venv" ]]; then
  python3.9 -m venv .venv
fi

source .venv/bin/activate

# The self-update pull below must authenticate to git, but the env files that
# define CR_AGENT_GIT_SSH_KEY_PATH are only sourced further down. Resolve the
# key path here (in a subshell, to avoid disturbing the authoritative source
# below) so production host pulls with its dedicated key. When unset (node1/local), git
# SSH behavior is left unchanged.
GIT_SSH_KEY_PATH="$(
  [[ -f "${COMMON_ENV_FILE}" ]] && source "${COMMON_ENV_FILE}"
  [[ -f "${ENV_FILE}" ]] && source "${ENV_FILE}"
  printf '%s' "${CR_AGENT_GIT_SSH_KEY_PATH:-}"
)"
if [[ -n "${GIT_SSH_KEY_PATH}" ]]; then
  export GIT_SSH_COMMAND="ssh -F /dev/null -i ${GIT_SSH_KEY_PATH} -o IdentitiesOnly=yes -o PreferredAuthentications=publickey -o StrictHostKeyChecking=accept-new"
fi

git pull --ff-only
python -m pip install -e '.[dev]'

if [[ ! -f "${COMMON_ENV_FILE}" ]]; then
  echo "missing common env file: ${COMMON_ENV_FILE}" >&2
  exit 1
fi

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "missing env file for deploy env '${DEPLOY_ENV}': ${ENV_FILE}" >&2
  exit 1
fi

set -a
source "${COMMON_ENV_FILE}"
source "${ENV_FILE}"
set +a

export CR_AGENT_DEPLOY_ENV="${DEPLOY_ENV}"

exec "${SCRIPT_DIR}/run_service.sh"

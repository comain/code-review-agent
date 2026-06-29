#!/usr/bin/env bash
set -euo pipefail

export CR_AGENT_HOOK_URL="${CR_AGENT_HOOK_URL:-http://127.0.0.1:8000/api/v1/hooks/trigger}"
export CR_AGENT_HOOK_TIMEOUT="${CR_AGENT_HOOK_TIMEOUT:-15}"
export CR_AGENT_HOOK_SKIP_USER="${CR_AGENT_HOOK_SKIP_USER:-reviewer}"
export CR_AGENT_GIT_HOST="${CR_AGENT_GIT_HOST:-github.com}"
export CR_AGENT_HOOK_TOKEN="${CR_AGENT_HOOK_TOKEN:-}"
export CR_AGENT_HOOK_LOG="${CR_AGENT_HOOK_LOG:-}"
export CR_AGENT_FORCE_TRIGGER_USER="${CR_AGENT_FORCE_TRIGGER_USER:-}"

log() {
  local message="$1"
  if [[ -n "${CR_AGENT_HOOK_LOG}" ]]; then
    printf '%s %s\n' "$(date '+%F %T')" "${message}" >> "${CR_AGENT_HOOK_LOG}"
  else
    printf '%s\n' "${message}" >&2
  fi
}

infer_repo_path() {
  local cwd
  cwd="$(pwd)"
  cwd="${cwd%/}"
  cwd="${cwd##*/}"
  if [[ "${cwd}" == *.git ]]; then
    cwd="${cwd%.git}"
  fi

  local repo_root
  repo_root="$(pwd)"
  repo_root="${repo_root%/.git}"
  if [[ "${repo_root}" == *.git ]]; then
    repo_root="${repo_root%.git}"
  fi
  if [[ "${repo_root}" == */* ]]; then
    printf '%s\n' "${repo_root##*/../}"
    return
  fi
  printf '%s\n' "${cwd}"
}

repo_path_from_pwd() {
  local pwd_path
  pwd_path="$(pwd)"
  pwd_path="${pwd_path%/.git}"
  if [[ "${pwd_path}" == *.git ]]; then
    pwd_path="${pwd_path%.git}"
  fi
  if [[ "${pwd_path}" =~ /repositories/(.+)$ ]]; then
    printf '%s\n' "${BASH_REMATCH[1]}"
    return
  fi
  if [[ "${pwd_path}" =~ /([^/]+)/([^/]+)$ ]]; then
    printf '%s/%s\n' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}"
    return
  fi
  printf '%s\n' "${pwd_path##*/}"
}

repo_path="${CR_AGENT_REPO_PATH:-$(repo_path_from_pwd)}"
repo_path="${repo_path#.}"
repo_path="${repo_path#/}"
repo_path="${repo_path%.git}"
group="${repo_path%%/*}"
project="${repo_path##*/}"
repo_url="${CR_AGENT_REPO_URL:-git@${CR_AGENT_GIT_HOST}:${repo_path}.git}"
app_name="${CR_AGENT_APP_NAME:-${project//-/_}}"

resolve_operator() {
  if [[ -n "${CR_AGENT_FORCE_TRIGGER_USER}" ]]; then
    printf '%s\n' "${CR_AGENT_FORCE_TRIGGER_USER}"
    return
  fi
  for candidate in "${GL_USERNAME:-}" "${GITLAB_USER_LOGIN:-}" "${USER_NAME:-}" "${PUSH_USER:-}"; do
    if [[ -n "${candidate}" ]]; then
      printf '%s\n' "${candidate}"
      return
    fi
  done
  printf '%s\n' ""
}

post_branch_update() {
  local branch="$1"
  local newrev="$2"
  local operator="$3"

  local payload
  payload="$(python3 - <<PY
import json
payload = {
    "app_name": ${app_name@Q},
    "repo_url": ${repo_url@Q},
    "branch": ${branch@Q},
    "commit_id": ${newrev@Q},
    "operator": ${operator@Q},
    "trigger_source": "git-hook",
    "metadata": {
        "scene": "git-hook",
        "group": ${group@Q},
        "repo_path": ${repo_path@Q},
    },
}
print(json.dumps(payload, ensure_ascii=False))
PY
)"

  local -a curl_args
  curl_args=(
    curl
    --silent
    --show-error
    --fail
    --max-time "${CR_AGENT_HOOK_TIMEOUT}"
    -H "Content-Type: application/json"
    -X POST
    --data "${payload}"
  )
  if [[ -n "${CR_AGENT_HOOK_TOKEN}" ]]; then
    curl_args+=(-H "X-CR-Agent-Token: ${CR_AGENT_HOOK_TOKEN}")
  fi
  curl_args+=("${CR_AGENT_HOOK_URL}")

  local response
  response="$("${curl_args[@]}")"
  log "triggered branch=${branch} commit=${newrev} operator=${operator:-unknown} response=${response}"
}

handle_update() {
  local oldrev="$1"
  local newrev="$2"
  local refname="$3"

  if [[ "${refname}" != refs/heads/* ]]; then
    log "skip ref=${refname} reason=not_branch"
    return
  fi
  if [[ "${newrev}" == "0000000000000000000000000000000000000000" ]]; then
    log "skip ref=${refname} reason=deleted"
    return
  fi

  local branch operator
  branch="${refname#refs/heads/}"
  operator="$(resolve_operator)"
  if [[ -n "${operator}" && "${operator}" == "${CR_AGENT_HOOK_SKIP_USER}" ]]; then
    log "skip branch=${branch} operator=${operator} reason=skip_user"
    return
  fi

  post_branch_update "${branch}" "${newrev}" "${operator}"
}

if [[ $# -eq 3 ]]; then
  handle_update "$1" "$2" "$3"
  exit 0
fi

while read -r oldrev newrev refname; do
  handle_update "${oldrev}" "${newrev}" "${refname}"
done

#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="${ROOT_DIR:-/opt/app/cr_agent}"
SCRIPT_PATH="$ROOT_DIR/scripts/concurrency_test.py"
OUTPUT_DIR="${OUTPUT_DIR:-$ROOT_DIR/runtime/benchmarks}"
MODEL="${MODEL:-llm-proxy/gpt-5.5}"
TOTAL="${TOTAL:-10}"
CONCURRENCY_SET="${CONCURRENCY_SET:-5,10}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-1200}"
OPENCODE_BIN="${OPENCODE_BIN:-opencode}"
OPENCODE_LOG_LEVEL="${OPENCODE_LOG_LEVEL:-INFO}"

SERVICE_A_REPO_PATH="${SERVICE_A_REPO_PATH:-$ROOT_DIR/runtime/repos/service_a}"
SERVICE_B_REPO_PATH="${SERVICE_B_REPO_PATH:-$ROOT_DIR/runtime/repos/service_b}"

SERVICE_A_PROMPT_FILE="${SERVICE_A_PROMPT_FILE:-}"
SERVICE_B_PROMPT_FILE="${SERVICE_B_PROMPT_FILE:-}"

DEFAULT_PROMPT='请对当前仓库做静态分析，只输出一个 JSON：{"summary":"...","pass_check":true,"score":100,"findings":[]}'

mkdir -p "$OUTPUT_DIR"

timestamp="$(date +%Y%m%d-%H%M%S)"
summary_file="$OUTPUT_DIR/opencode-benchmark-summary-$timestamp.log"

run_case() {
  local name="$1"
  local repo_path="$2"
  local prompt_file="$3"
  local concurrency="$4"
  local log_file="$OUTPUT_DIR/${name}-c${concurrency}-$timestamp.log"

  {
    echo "================================================================"
    echo "case=$name concurrency=$concurrency total=$TOTAL model=$MODEL"
    echo "repo_path=$repo_path"
    echo "started_at=$(date '+%F %T')"
    if [[ -n "$prompt_file" ]]; then
      echo "prompt_file=$prompt_file"
    else
      echo "prompt_file=<inline default prompt>"
    fi
    echo "log_file=$log_file"
    echo "================================================================"
  } | tee -a "$summary_file" "$log_file"

  if [[ ! -d "$repo_path" ]]; then
    echo "skip: repo path not found: $repo_path" | tee -a "$summary_file" "$log_file"
    return 0
  fi

  if [[ -n "$prompt_file" ]]; then
    python3 "$SCRIPT_PATH" \
      --mode opencode \
      --total "$TOTAL" \
      --submit-concurrency "$concurrency" \
      --timeout-seconds "$TIMEOUT_SECONDS" \
      --opencode-bin "$OPENCODE_BIN" \
      --opencode-log-level "$OPENCODE_LOG_LEVEL" \
      --opencode-repo-path "$repo_path" \
      --opencode-model "$MODEL" \
      --opencode-prompt-file "$prompt_file" | tee -a "$summary_file" "$log_file"
  else
    python3 "$SCRIPT_PATH" \
      --mode opencode \
      --total "$TOTAL" \
      --submit-concurrency "$concurrency" \
      --timeout-seconds "$TIMEOUT_SECONDS" \
      --opencode-bin "$OPENCODE_BIN" \
      --opencode-log-level "$OPENCODE_LOG_LEVEL" \
      --opencode-repo-path "$repo_path" \
      --opencode-model "$MODEL" \
      --opencode-prompt "$DEFAULT_PROMPT" | tee -a "$summary_file" "$log_file"
  fi

  echo | tee -a "$summary_file" "$log_file"
}

IFS=',' read -r -a concurrencies <<< "$CONCURRENCY_SET"

for concurrency in "${concurrencies[@]}"; do
  run_case "service_a" "$SERVICE_A_REPO_PATH" "$SERVICE_A_PROMPT_FILE" "$concurrency"
  run_case "service_b" "$SERVICE_B_REPO_PATH" "$SERVICE_B_PROMPT_FILE" "$concurrency"
done

echo "summary=$summary_file"

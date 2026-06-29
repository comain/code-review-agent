#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${PROJECT_ROOT}"
source .venv/bin/activate

HOST="${MOCK_CALLBACK_HOST:-0.0.0.0}"
PORT="${MOCK_CALLBACK_PORT:-9999}"
OUTPUT="${MOCK_CALLBACK_OUTPUT:-runtime/mock_callback_requests.jsonl}"

echo "PROJECT_ROOT=${PROJECT_ROOT}"
echo "MOCK_CALLBACK_HOST=${HOST}"
echo "MOCK_CALLBACK_PORT=${PORT}"
echo "MOCK_CALLBACK_OUTPUT=${OUTPUT}"

exec python scripts/mock_callback_server.py --host "${HOST}" --port "${PORT}" --output "${OUTPUT}"

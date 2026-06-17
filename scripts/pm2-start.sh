#!/usr/bin/env bash
# pm2 用 API 起動スクリプト（reload なしの本番向け）
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ -x "$ROOT/.venv/bin/uvicorn" ]]; then
  exec "$ROOT/.venv/bin/uvicorn" api:app --host 0.0.0.0 --port 8089
fi

exec poetry run uvicorn api:app --host 0.0.0.0 --port 8089

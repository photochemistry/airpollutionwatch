#!/usr/bin/env bash
# pm2 用 API 起動スクリプト（reload なしの本番向け）
set -euo pipefail
cd "$(dirname "$0")/.."
exec poetry run uvicorn api:app --host 0.0.0.0 --port 8089

#!/bin/bash
# airpollutionwatch API 死活監視の cron ジョブを設定する（5分間隔、andersan-api と同様）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HEALTHCHECK_SCRIPT="$SCRIPT_DIR/healthcheck-api.sh"
CRON_JOB="2-59/5 * * * * $HEALTHCHECK_SCRIPT"
MARKER="airpollutionwatch-api/scripts/healthcheck-api.sh"

if [[ ! -x "$HEALTHCHECK_SCRIPT" ]]; then
    chmod +x "$HEALTHCHECK_SCRIPT"
fi

echo "=== airpollutionwatch API 死活監視 cron のセットアップ ==="

if crontab -l 2>/dev/null | grep -q "$MARKER"; then
    echo "既存の cron ジョブがあります。更新します。"
    crontab -l 2>/dev/null | grep -v "$MARKER" | crontab -
fi

(crontab -l 2>/dev/null; echo "$CRON_JOB") | crontab -

echo "cron ジョブを設定しました。"
echo "スケジュール: 5分ごと（2,7,12,...分）"
echo "コマンド: $CRON_JOB"
echo ""
echo "現在の cron ジョブ一覧:"
crontab -l

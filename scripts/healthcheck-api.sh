#!/bin/bash
# localhost:8089（airpollutionwatch API）の死活監視。
# 成功時のみ Healthchecks.io に ping する。
set -euo pipefail

API_URL="${APW_API_URL:-http://localhost:8089/openapi.json}"
HC_PING_URL="${APW_HC_PING_URL:-https://hc-ping.com/d5acc9c9-85fe-47e4-8632-133a5fd02b66}"

curl -fsS --connect-timeout 5 --max-time 15 "$API_URL" > /dev/null
curl -fsS --connect-timeout 10 --max-time 15 "$HC_PING_URL"

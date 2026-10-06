#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export XYMB_RUNTIME="${XYMB_RUNTIME:-$(realpath ../runtime/xianyu-management-bot)}"
export XYMB_ENV_FILE="${XYMB_ENV_FILE:-$XYMB_RUNTIME/local.env}"
if [[ ! -f "$XYMB_ENV_FILE" ]]; then
  echo "缺少私有运行配置：$XYMB_ENV_FILE" >&2
  exit 1
fi
mkdir -p data logs backups static/uploads trajectory_history browser_data
exec docker compose -p xymb-gudong -f compose.local.yml "$@"

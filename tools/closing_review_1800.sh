#!/usr/bin/env bash
# 盘后复盘 Agent - 每日 18:00

export PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin"
export LANG="zh_CN.UTF-8"

set -eu

PROJ=~/src/a-stock-trading
LOG=~/shared/trading/logs/closing_review_$(date +%Y-%m-%d).log
mkdir -p "$(dirname "$LOG")"

# 非交易日 → 跳过
if ! python3 "$PROJ/tools/is_trading_day.py"; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 非交易日，跳过" >> "$LOG"
    exit 0
fi

cd "$PROJ"
echo "[$(date '+%H:%M:%S')] closing_review 开始" >> "$LOG"
python3 -m trading_agent.intraday closing_review >> "$LOG" 2>&1
echo "[$(date '+%H:%M:%S')] closing_review 完成" >> "$LOG"

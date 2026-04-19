#!/usr/bin/env bash
# 盘后 Layer 1 + Layer 2 分析 - 每日 15:10

export PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin"
export LANG="zh_CN.UTF-8"

set -eu

PROJ=~/src/a-stock-trading
LOG=~/shared/trading/logs/layered_analysis_$(date +%Y-%m-%d).log
mkdir -p "$(dirname "$LOG")"

# 非交易日 → 跳过
if ! python3 "$PROJ/tools/is_trading_day.py"; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 非交易日，跳过" >> "$LOG"
    exit 0
fi

cd "$PROJ"
echo "[$(date '+%H:%M:%S')] layered_analysis 开始" >> "$LOG"
python3 -m trading_agent.intraday.layered_analysis >> "$LOG" 2>&1
echo "[$(date '+%H:%M:%S')] layered_analysis 完成" >> "$LOG"

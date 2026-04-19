#!/usr/bin/env bash
# 盘中监控 - 每分钟调度一次
# LaunchAgent 配置 StartInterval=60，本脚本自行判断时段和交易日

export PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin"
export LANG="zh_CN.UTF-8"

set -eu

PROJ=~/src/a-stock-trading
LOG=~/shared/trading/logs/intraday_monitor_$(date +%Y-%m-%d).log
mkdir -p "$(dirname "$LOG")"

# 非交易日 → 静默退出
if ! python3 "$PROJ/tools/is_trading_day.py"; then
    exit 0
fi

# 盘中时段：09:25-11:30 或 12:59-15:00
HM=$(date +%H%M)
if [ "$HM" -lt "0925" ] || [ "$HM" -gt "1500" ]; then
    exit 0
fi
# 午休跳过（11:31-12:58）
if [ "$HM" -gt "1130" ] && [ "$HM" -lt "1259" ]; then
    exit 0
fi

cd "$PROJ"
echo "[$(date '+%H:%M:%S')] intraday monitor tick" >> "$LOG"
python3 -m trading_agent.intraday.monitor >> "$LOG" 2>&1 || true

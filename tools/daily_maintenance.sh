#!/usr/bin/env bash
# 每日盘后维护脚本 — 数据体检 + 健康度监控 + 策略评估
#
# 定时：每日 17:00 周一至周五（盘后数据拉完后）
# crontab:
#   0 17 * * 1-5 /Users/luoxin/src/a-stock-trading/tools/daily_maintenance.sh
#
# 通过飞书告警（需要 Agent 环境才能发送消息）

# cron 环境 PATH 精简，显式声明防止 python3 找不到
export PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin"
export LANG="zh_CN.UTF-8"

set -eu

PROJ_ROOT=~/src/a-stock-trading
LOG_DIR=~/shared/trading/logs
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/daily_maintenance_$(date +%Y-%m-%d).log"

cd "$PROJ_ROOT"

log() {
    echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

# === Step 1: 数据质量修复（幂等） ===
log "=== Step 1: 数据质量修复 ==="
python3 data/data_quality_fix.py 2>&1 | tee -a "$LOG_FILE" || true

# === Step 2: 最近 7 天数据健康度审计 ===
log ""
log "=== Step 2: 数据质量体检 ==="
if python3 data/data_quality_audit.py --days 7 2>&1 | tee -a "$LOG_FILE"; then
    log "  ✓ 数据健康"
else
    log "  ⚠️ 数据体检发现问题（见上方输出）"
fi

# === Step 3: 策略健康度监控 ===
log ""
log "=== Step 3: 策略健康度监控 ==="
if python3 tools/strategy_health.py --windows 5 20 60 --alert 2>&1 | tee -a "$LOG_FILE"; then
    log "  ✓ 策略健康度正常"
else
    log "  ⚠️ 策略健康度有告警（见 /tmp/strategy_health_alert.txt）"
fi

# === Step 4: 每周一跑全量 limit_up 重建（防漂移） ===
if [ "$(date +%u)" = "1" ]; then
    log ""
    log "=== Step 4（周一）: limit_up 全量重建 ==="
    START=$(date -v-60d +%Y-%m-%d)
    END=$(date +%Y-%m-%d)
    python3 data/rebuild_limit_up.py --start "$START" --end "$END" \
        2>&1 | tee -a "$LOG_FILE" || true
fi

log ""
log "=== 维护任务完成 ==="

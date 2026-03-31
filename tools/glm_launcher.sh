#!/bin/bash
# GLM Coding Pro 双账号抢单启动器
# 由 launchd 在补货前5分钟调用，两个账号并行抢

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WORKSPACE="$(dirname "$SCRIPT_DIR")"
LOG_DIR="$WORKSPACE/logs"
mkdir -p "$LOG_DIR"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

echo "[$(date)] GLM 抢单启动" >> "$LOG_DIR/glm_sniper.log"

# 账号1（后台）
GLM_AUTH_FILE="$WORKSPACE/bigmodel_auth.json" \
GLM_USER_LABEL="Account1" \
python3 "$SCRIPT_DIR/glm_sniper.py" >> "$LOG_DIR/glm_sniper_account1_${TIMESTAMP}.log" 2>&1 &
PID1=$!

# 账号2（后台）
GLM_AUTH_FILE="$WORKSPACE/bigmodel_auth_zym.json" \
GLM_USER_LABEL="Account2" \
python3 "$SCRIPT_DIR/glm_sniper.py" >> "$LOG_DIR/glm_sniper_account2_${TIMESTAMP}.log" 2>&1 &
PID2=$!

echo "[$(date)] 账号1 PID=$PID1, 账号2 PID=$PID2" >> "$LOG_DIR/glm_sniper.log"

# 等待两个进程完成
wait $PID1 $PID2
echo "[$(date)] GLM 抢单结束" >> "$LOG_DIR/glm_sniper.log"

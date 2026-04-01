#!/bin/bash
# GLM Coding Pro 双账号抢单启动器
# 由 launchd 在补货前5分钟调用，两个账号并行抢

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# 数据目录（认证文件、日志等），可通过环境变量覆盖
DATA_DIR="${GLM_DATA_DIR:-$HOME/src/happyclaw/data/groups/main}"
LOG_DIR="$DATA_DIR/trading/logs"
mkdir -p "$LOG_DIR"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

echo "[$(date)] GLM 抢单启动" >> "$LOG_DIR/glm_sniper.log"

# 账号1 罗鑫 — 包年Pro ¥1430.4
GLM_AUTH_FILE="$DATA_DIR/bigmodel_auth.json" \
GLM_USER_LABEL="罗鑫" \
GLM_PRODUCT_ID="product-5643e6" \
GLM_PRODUCT_NAME="GLM Coding Pro 包年" \
GLM_RESULT_FILE="$DATA_DIR/trading/glm_result_1.json" \
python3 "$SCRIPT_DIR/glm_sniper.py" >> "$LOG_DIR/glm_sniper_account1_${TIMESTAMP}.log" 2>&1 &
PID1=$!

# 账号2 ZYM — 包月Pro ¥149
GLM_AUTH_FILE="$DATA_DIR/bigmodel_auth_zym.json" \
GLM_USER_LABEL="ZYM" \
GLM_PRODUCT_ID="product-1df3e1" \
GLM_PRODUCT_NAME="GLM Coding Pro 包月" \
GLM_RESULT_FILE="$DATA_DIR/trading/glm_result_2.json" \
python3 "$SCRIPT_DIR/glm_sniper.py" >> "$LOG_DIR/glm_sniper_account2_${TIMESTAMP}.log" 2>&1 &
PID2=$!

echo "[$(date)] 账号1 PID=$PID1, 账号2 PID=$PID2" >> "$LOG_DIR/glm_sniper.log"

# 等待两个进程完成
wait $PID1 $PID2
echo "[$(date)] GLM 抢单结束" >> "$LOG_DIR/glm_sniper.log"

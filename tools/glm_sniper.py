#!/usr/bin/env python3
"""
智谱 GLM Coding Plan 抢单脚本
在补货时刻自动轮询 → 检测放开 → 下单 → 结果写入 JSON 文件
"""

import json
import os
import sys
import time
import requests
from datetime import datetime

# ═══════════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════════

API_BASE = "https://bigmodel.cn/api"
PRODUCT_ID = os.environ.get("GLM_PRODUCT_ID", "product-a6ef45")  # 默认 Pro 套餐
PRODUCT_NAME = os.environ.get("GLM_PRODUCT_NAME", "GLM Coding Pro")
USER_LABEL = os.environ.get("GLM_USER_LABEL", "User")

# Token 来源：环境变量 > 浏览器状态文件
TOKEN_DIRECT = os.environ.get("GLM_TOKEN", "")
AUTH_FILE = os.environ.get("GLM_AUTH_FILE", os.path.join(os.path.dirname(__file__), "..", "bigmodel_auth.json"))

# 结果文件
RESULT_FILE = os.environ.get("GLM_RESULT_FILE", os.path.join(os.path.dirname(__file__), "glm_result.json"))

# 轮询配置
POLL_INTERVAL = 0.3       # 放开后的轮询间隔（秒）
PRE_POLL_INTERVAL = 1.0   # 放开前的轮询间隔（秒）
MAX_RETRY = 5             # 下单重试次数
MAX_WAIT_MINUTES = 30     # 最大等待时间（分钟）


def log(msg):
    """带时间戳的日志输出（立即刷新）"""
    print("[%s][%s] %s" % (datetime.now().strftime("%H:%M:%S"), USER_LABEL, msg), flush=True)


def load_token():
    """加载 JWT token（环境变量优先，其次从浏览器状态文件提取）"""
    if TOKEN_DIRECT:
        log("✅ 使用环境变量 Token（长度 %d）" % len(TOKEN_DIRECT))
        return TOKEN_DIRECT

    if not os.path.exists(AUTH_FILE):
        log("❌ 未找到认证文件: %s" % AUTH_FILE)
        sys.exit(1)

    with open(AUTH_FILE) as f:
        state = json.load(f)

    cookies = state.get("cookies", [])
    for c in cookies:
        if c.get("name") == "bigmodel_token_production":
            token = c["value"]
            log("✅ Token 加载成功（长度 %d）" % len(token))
            return token

    log("❌ 未在状态文件中找到 bigmodel_token_production cookie")
    sys.exit(1)


def get_headers(token):
    return {
        "Authorization": "Bearer %s" % token,
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
        "Referer": "https://bigmodel.cn/glm-coding",
    }


# ═══════════════════════════════════════════════════════════════
# 结果写入
# ═══════════════════════════════════════════════════════════════

def save_result(status, message, **extra):
    """将结果写入 JSON 文件，供 HappyClaw 轮询读取"""
    result = {
        "user": USER_LABEL,
        "product": PRODUCT_NAME,
        "status": status,  # "success" | "failed" | "timeout" | "error"
        "message": message,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        **extra,
    }
    tmp = RESULT_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    os.rename(tmp, RESULT_FILE)
    log("结果已写入: %s" % RESULT_FILE)


# ═══════════════════════════════════════════════════════════════
# API 调用
# ═══════════════════════════════════════════════════════════════

def check_available(headers):
    """检查是否可购买（preview 接口）"""
    try:
        resp = requests.post(
            "%s/biz/pay/preview" % API_BASE,
            headers=headers,
            json={"productId": PRODUCT_ID},
            timeout=5,
        )
        data = resp.json()
        if data.get("code") == 200 and data.get("data"):
            sold_out = data["data"].get("soldOut", True)
            pay_amount = data["data"].get("payAmount")
            biz_id = data["data"].get("bizId")
            return not sold_out, pay_amount, biz_id, data["data"]
        return False, None, None, data
    except Exception as e:
        log("[preview] 异常: %s" % e)
        return False, None, None, None


def create_sign(headers):
    """创建签约订阅"""
    try:
        resp = requests.post(
            "%s/biz/pay/create-sign" % API_BASE,
            headers=headers,
            json={"productId": PRODUCT_ID},
            timeout=10,
        )
        data = resp.json()
        log("[create-sign] 响应: %s" % json.dumps(data, ensure_ascii=False))
        return data
    except Exception as e:
        log("[create-sign] 异常: %s" % e)
        return None


def check_limit_buy(headers):
    """检查限购状态"""
    try:
        resp = requests.get(
            "%s/biz/product/isLimitBuy" % API_BASE,
            headers=headers,
            timeout=5,
        )
        data = resp.json()
        if data.get("code") == 200:
            return data.get("data", {})
    except Exception as e:
        log("[isLimitBuy] 异常: %s" % e)
    return {}


# ═══════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════

def main():
    log("脚本启动 | 目标: %s (%s)" % (PRODUCT_NAME, PRODUCT_ID))

    token = load_token()
    headers = get_headers(token)

    # 验证 token 有效性
    limit_info = check_limit_buy(headers)
    if not limit_info:
        log("❌ Token 无效或已过期")
        save_result("error", "Token 无效或已过期，请重新登录")
        sys.exit(1)
    log("✅ Token 验证通过 | 限购: %s" % limit_info)

    # 初始检查
    available, pay_amount, biz_id, preview_data = check_available(headers)
    if available:
        log("🎉 当前已可购买！直接下单")
    else:
        log("⏳ 当前售罄，开始轮询等待...")

    # 轮询等待放开（带超时）
    poll_count = 0
    start_time = time.time()
    while not available:
        elapsed = time.time() - start_time
        if elapsed > MAX_WAIT_MINUTES * 60:
            log("⏰ 等待超时（%d 分钟），退出" % MAX_WAIT_MINUTES)
            save_result("timeout", "等待 %d 分钟后仍未补货" % MAX_WAIT_MINUTES)
            sys.exit(0)

        now = datetime.now()
        if now.hour == 9 and now.minute >= 59 and now.second >= 50:
            interval = POLL_INTERVAL
        elif now.hour >= 10:
            interval = POLL_INTERVAL
        else:
            interval = PRE_POLL_INTERVAL

        time.sleep(interval)
        available, pay_amount, biz_id, preview_data = check_available(headers)
        poll_count += 1

        # 每10次输出一次状态（高频轮询时约3秒一次）
        if poll_count % 10 == 0:
            log("轮询 #%d | 已等 %ds | 售罄 | interval=%.1fs" % (poll_count, int(elapsed), interval))

    # 放开了！
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    log("🎉 检测到放开！价格: ¥%s | 轮询 %d 次" % (pay_amount, poll_count))
    log("preview: %s" % json.dumps(preview_data, ensure_ascii=False))

    # 立即下单
    for attempt in range(1, MAX_RETRY + 1):
        log("[下单] 第 %d/%d 次尝试..." % (attempt, MAX_RETRY))
        result = create_sign(headers)

        if result and result.get("code") == 200:
            order_data = result.get("data", {})
            log("✅ 下单成功！")
            log("订单: %s" % json.dumps(order_data, ensure_ascii=False))

            pay_url = order_data.get("payUrl") or order_data.get("url") or order_data.get("signUrl")
            save_result("success", "下单成功", price=pay_amount, detect_time=ts, pay_url=pay_url, order_data=order_data)
            return

        elif result and result.get("code") == 500:
            log("[下单] 失败: %s" % result.get("msg"))
            if attempt < MAX_RETRY:
                time.sleep(0.2)
        else:
            log("[下单] 未知响应: %s" % result)
            if attempt < MAX_RETRY:
                time.sleep(0.3)

    # 全部重试失败
    log("❌ 下单失败，已重试 %d 次" % MAX_RETRY)
    save_result("failed", "重试 %d 次均未成功" % MAX_RETRY)


if __name__ == "__main__":
    main()

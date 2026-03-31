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


def load_token():
    """加载 JWT token（环境变量优先，其次从浏览器状态文件提取）"""
    if TOKEN_DIRECT:
        print("[%s] ✅ 使用环境变量 Token（长度 %d）" % (USER_LABEL, len(TOKEN_DIRECT)))
        return TOKEN_DIRECT

    if not os.path.exists(AUTH_FILE):
        print("[%s] ❌ 未找到认证文件: %s" % (USER_LABEL, AUTH_FILE))
        print("   请先运行浏览器登录并保存状态")
        sys.exit(1)

    with open(AUTH_FILE) as f:
        state = json.load(f)

    cookies = state.get("cookies", [])
    for c in cookies:
        if c.get("name") == "bigmodel_token_production":
            token = c["value"]
            print("[%s] ✅ Token 加载成功（长度 %d）" % (USER_LABEL, len(token)))
            return token

    print("[%s] ❌ 未在状态文件中找到 bigmodel_token_production cookie" % USER_LABEL)
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
    print("[%s] 结果已写入: %s" % (USER_LABEL, RESULT_FILE))


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
    except Exception as e:
        print("  [preview] 异常: %s" % e)
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
        print("  [create-sign] 响应: %s" % json.dumps(data, ensure_ascii=False))
        return data
    except Exception as e:
        print("  [create-sign] 异常: %s" % e)
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
        print("  [isLimitBuy] 异常: %s" % e)
    return {}


# ═══════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════

def main():
    token = load_token()
    headers = get_headers(token)

    # 验证 token 有效性
    limit_info = check_limit_buy(headers)
    if not limit_info:
        print("[%s] ❌ Token 无效或已过期" % USER_LABEL)
        save_result("error", "Token 无效或已过期，请重新登录")
        sys.exit(1)
    print("[%s] ✅ Token 验证通过，限购状态: %s" % (USER_LABEL, limit_info))

    # 初始检查
    available, pay_amount, biz_id, preview_data = check_available(headers)
    if available:
        print("[%s] 🎉 当前已可购买！直接下单" % USER_LABEL)
    else:
        print("[%s] ⏳ 当前售罄，等待放开..." % USER_LABEL)
        print("   目标: %s (%s)" % (PRODUCT_NAME, PRODUCT_ID))

    # 轮询等待放开（带超时）
    poll_count = 0
    start_time = time.time()
    while not available:
        if time.time() - start_time > MAX_WAIT_MINUTES * 60:
            print("[%s] ⏰ 等待超时（%d 分钟），退出" % (USER_LABEL, MAX_WAIT_MINUTES))
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

        if poll_count % 30 == 0:
            print("[%s][%s] 已轮询 %d 次，仍售罄" % (USER_LABEL, now.strftime("%H:%M:%S"), poll_count))

    # 放开了！
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print("[%s] 🎉 [%s] 检测到放开！价格: ¥%s" % (USER_LABEL, ts, pay_amount))
    print("   preview 完整数据: %s" % json.dumps(preview_data, ensure_ascii=False))

    # 立即下单
    for attempt in range(1, MAX_RETRY + 1):
        print("[%s] [下单] 第 %d 次尝试..." % (USER_LABEL, attempt))
        result = create_sign(headers)

        if result and result.get("code") == 200:
            order_data = result.get("data", {})
            print("[%s] ✅ 下单成功！" % USER_LABEL)
            print("   订单数据: %s" % json.dumps(order_data, ensure_ascii=False))

            pay_url = order_data.get("payUrl") or order_data.get("url") or order_data.get("signUrl")
            save_result("success", "下单成功", price=pay_amount, detect_time=ts, pay_url=pay_url, order_data=order_data)
            return

        elif result and result.get("code") == 500:
            print("[%s] [下单] 失败: %s" % (USER_LABEL, result.get("msg")))
            if attempt < MAX_RETRY:
                time.sleep(0.2)
        else:
            print("[%s] [下单] 未知响应: %s" % (USER_LABEL, result))
            if attempt < MAX_RETRY:
                time.sleep(0.3)

    # 全部重试失败
    print("[%s] ❌ 下单失败，已重试 %d 次" % (USER_LABEL, MAX_RETRY))
    save_result("failed", "重试 %d 次均未成功" % MAX_RETRY)


if __name__ == "__main__":
    main()

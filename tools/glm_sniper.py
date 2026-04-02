#!/usr/bin/env python3
"""
智谱 GLM Coding Plan 抢单脚本
在补货时刻自动轮询 → 检测放开 → 下单 → 结果写入 JSON 文件
"""

import json
import os
import random
import sys
import time
import threading
import requests
from datetime import datetime

# ═══════════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════════

API_BASE = "https://bigmodel.cn/api"
PRODUCT_ID = os.environ.get("GLM_PRODUCT_ID", "product-1df3e1")  # GLM Coding Pro ¥149/月
PRODUCT_NAME = os.environ.get("GLM_PRODUCT_NAME", "GLM Coding Pro")
USER_LABEL = os.environ.get("GLM_USER_LABEL", "User")

# Token 来源：环境变量 > 浏览器状态文件
TOKEN_DIRECT = os.environ.get("GLM_TOKEN", "")
AUTH_FILE = os.environ.get("GLM_AUTH_FILE", os.path.join(os.path.dirname(__file__), "..", "bigmodel_auth.json"))

# 结果文件
RESULT_FILE = os.environ.get("GLM_RESULT_FILE", os.path.join(os.path.dirname(__file__), "glm_result.json"))

# 抢单配置
SNIPE_DURATION = 60       # 持续抢单秒数（从开抢时刻起）
SNIPE_THREADS = int(os.environ.get("GLM_THREADS", "8"))  # 并发下单线程数
SNIPE_HOUR = int(os.environ.get("GLM_SNIPE_HOUR", "10"))   # 补货小时
SNIPE_MINUTE = int(os.environ.get("GLM_SNIPE_MINUTE", "0")) # 补货分钟
PRE_START_SEC = 10        # 提前多少秒开始发请求（9:59:50 开始）


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

def check_available(headers, tag=""):
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
        log("%s[preview] 异常: %s" % (tag, e))
        return False, None, None, None


def create_sign(headers, tag=""):
    """创建签约订阅"""
    try:
        resp = requests.post(
            "%s/biz/pay/create-sign" % API_BASE,
            headers=headers,
            json={"productId": PRODUCT_ID},
            timeout=10,
        )
        data = resp.json()
        log("%s[create-sign] code=%s msg=%s" % (tag, data.get("code"), data.get("msg", "")))
        return data
    except Exception as e:
        log("%s[create-sign] 异常: %s" % (tag, e))
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
    log("脚本启动 | 目标: %s (%s) | %d 线程" % (PRODUCT_NAME, PRODUCT_ID, SNIPE_THREADS))

    token = load_token()
    headers = get_headers(token)

    # 验证 token 有效性
    limit_info = check_limit_buy(headers)
    if not limit_info:
        log("❌ Token 无效或已过期")
        save_result("error", "Token 无效或已过期，请重新登录")
        sys.exit(1)
    log("✅ Token 验证通过 | 限购: %s" % limit_info)

    # 共享状态
    success_event = threading.Event()
    success_result = [None]
    success_meta = [None]

    # 等待到开抢时间（补货时间前 PRE_START_SEC 秒）
    now = datetime.now()
    snipe_time = now.replace(hour=SNIPE_HOUR, minute=SNIPE_MINUTE, second=0, microsecond=0)
    start_fire = snipe_time - __import__("datetime").timedelta(seconds=PRE_START_SEC)

    if now < start_fire:
        wait_sec = (start_fire - now).total_seconds()
        log("等待到 %s 开始抢单（还有 %ds）..." % (start_fire.strftime("%H:%M:%S"), int(wait_sec)))
        time.sleep(wait_sec)
    else:
        log("已过开抢时间，立即开始！")

    fire_time = time.time()
    log("🔥 开抢！%d 线程全部发射 create-sign，持续 %ds" % (SNIPE_THREADS, SNIPE_DURATION))

    def order_worker(thread_id):
        """下单线程：持续发 create-sign 直到成功或超时"""
        tag = "[T%d] " % thread_id
        # 每个线程随机错开 0~100ms
        time.sleep(random.uniform(0, 0.1))
        attempt = 0

        while time.time() - fire_time < SNIPE_DURATION:
            if success_event.is_set():
                log("%s其他线程已成功，停止" % tag)
                return

            attempt += 1
            result = create_sign(headers, tag)

            if result and result.get("code") == 200:
                order_data = result.get("data", {})
                ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                log("%s✅ 第 %d 次下单成功！订单: %s" % (tag, attempt, json.dumps(order_data, ensure_ascii=False)))
                success_result[0] = order_data
                success_meta[0] = (thread_id, ts)
                success_event.set()
                return
            else:
                msg = result.get("msg") if result else "无响应"
                code = result.get("code") if result else "N/A"
                if attempt <= 3 or attempt % 20 == 0:
                    log("%s第 %d 次 | code=%s | %s" % (tag, attempt, code, msg))
                # 随机抖动，避免所有线程同步撞服务器
                time.sleep(random.uniform(0.03, 0.2))

        log("%s抢单结束 | 共 %d 次 | 耗时 %ds | 未成功" % (tag, attempt, int(time.time() - fire_time)))

    # 启动所有下单线程
    threads = []
    for i in range(SNIPE_THREADS):
        t = threading.Thread(target=order_worker, args=(i + 1,), daemon=True)
        threads.append(t)
        t.start()

    # 等待结束
    for t in threads:
        t.join(timeout=SNIPE_DURATION + 30)

    if success_event.is_set() and success_result[0]:
        order_data = success_result[0]
        tid, ts = success_meta[0]
        pay_url = order_data.get("payUrl") or order_data.get("url") or order_data.get("signUrl")
        save_result("success", "T%d 下单成功（%d线程并发）" % (tid, SNIPE_THREADS),
                    detect_time=ts, pay_url=pay_url, order_data=order_data)
    else:
        total_sec = int(time.time() - fire_time)
        log("❌ 全部 %d 线程抢单 %ds 未成功" % (SNIPE_THREADS, total_sec))
        save_result("failed", "%d线程抢单%ds未成功" % (SNIPE_THREADS, total_sec))


if __name__ == "__main__":
    main()

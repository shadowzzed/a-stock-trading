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

# 轮询配置
POLL_INTERVAL = 0.3       # 放开后的轮询间隔（秒）
PRE_POLL_INTERVAL = 1.0   # 放开前的轮询间隔（秒）
SNIPE_DURATION = 30       # 检测到放开后持续抢单的秒数（不限次数）
MAX_WAIT_MINUTES = 30     # 最大等待时间（分钟）
SNIPE_THREADS = int(os.environ.get("GLM_THREADS", "8"))  # 并发线程数（2检测 + 6下单）
DETECT_THREADS = 2        # 检测线程数
ORDER_THREADS = SNIPE_THREADS - DETECT_THREADS  # 下单线程数


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
    success_event = threading.Event()   # 任一线程成功则 set
    timeout_event = threading.Event()   # 超时则 set
    open_event = threading.Event()      # 检测到放开则 set，唤醒下单线程
    success_result = [None]             # 成功的订单数据
    success_meta = [None]               # 成功的元信息 (thread_id, pay_amount, ts)
    open_info = [None, None]            # [pay_amount, detect_time]
    start_time = time.time()

    def detect_worker(thread_id):
        """检测线程：轮询 preview 接口，发现放开后通知下单线程"""
        tag = "[D%d] " % thread_id
        poll_count = 0
        time.sleep(thread_id * 0.05)
        log("%s检测线程启动" % tag)

        while not success_event.is_set() and not timeout_event.is_set():
            elapsed = time.time() - start_time
            if elapsed > MAX_WAIT_MINUTES * 60:
                timeout_event.set()
                log("%s超时退出" % tag)
                return

            available, pay_amount, biz_id, preview_data = check_available(headers, tag)
            poll_count += 1

            if available:
                ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                log("%s🎉 检测到【可购买】！价格: ¥%s | 轮询 #%d | 耗时 %ds" % (tag, pay_amount, poll_count, int(elapsed)))
                open_info[0] = pay_amount
                open_info[1] = ts
                open_event.set()  # 唤醒所有下单线程

                # 检测线程也参与下单
                result = create_sign(headers, tag)
                if result and result.get("code") == 200:
                    order_data = result.get("data", {})
                    log("%s✅ 下单成功！订单: %s" % (tag, json.dumps(order_data, ensure_ascii=False)))
                    success_result[0] = order_data
                    success_meta[0] = (thread_id, pay_amount, ts)
                    success_event.set()
                    return
                else:
                    log("%s下单未成功: %s，继续检测..." % (tag, result.get("msg") if result else "无响应"))
            else:
                if poll_count % 50 == 0:
                    log("%s检测 #%d | 已等 %ds | 售罄" % (tag, poll_count, int(elapsed)))

                now = datetime.now()
                if now.hour == 9 and now.minute >= 59 and now.second >= 50:
                    interval = POLL_INTERVAL
                elif now.hour >= 10:
                    interval = POLL_INTERVAL
                else:
                    interval = PRE_POLL_INTERVAL
                time.sleep(interval)

        log("%s退出" % tag)

    def order_worker(thread_id):
        """下单线程：等待检测线程信号后持续抢单"""
        tag = "[O%d] " % thread_id
        log("%s下单线程就绪，等待放开信号..." % tag)

        # 等待检测线程发现放开
        while not success_event.is_set() and not timeout_event.is_set():
            if open_event.wait(timeout=1):
                break

        if success_event.is_set() or timeout_event.is_set():
            log("%s退出（%s）" % (tag, "已成功" if success_event.is_set() else "超时"))
            return

        # 收到放开信号，持续抢单 SNIPE_DURATION 秒
        snipe_start = time.time()
        attempt = 0
        log("%s收到放开信号，开始持续抢单（%ds）..." % (tag, SNIPE_DURATION))

        while time.time() - snipe_start < SNIPE_DURATION:
            if success_event.is_set():
                log("%s其他线程已成功，停止" % tag)
                return

            attempt += 1
            result = create_sign(headers, tag)

            if result and result.get("code") == 200:
                order_data = result.get("data", {})
                log("%s✅ 第%d次下单成功！订单: %s" % (tag, attempt, json.dumps(order_data, ensure_ascii=False)))
                success_result[0] = order_data
                success_meta[0] = (thread_id, open_info[0], open_info[1])
                success_event.set()
                return
            else:
                msg = result.get("msg") if result else "无响应"
                if attempt % 10 == 0:
                    log("%s已尝试 %d 次 | 最近错误: %s" % (tag, attempt, msg))
                # 随机抖动间隔，避免所有线程同时撞服务器
                time.sleep(random.uniform(0.05, 0.25))

        log("%s抢单 %d 次均未成功（%ds），退出" % (tag, attempt, SNIPE_DURATION))

    # 启动线程
    log("启动 %d 检测 + %d 下单 = %d 线程" % (DETECT_THREADS, ORDER_THREADS, SNIPE_THREADS))
    threads = []

    for i in range(DETECT_THREADS):
        t = threading.Thread(target=detect_worker, args=(i + 1,), daemon=True)
        threads.append(t)
        t.start()

    for i in range(ORDER_THREADS):
        t = threading.Thread(target=order_worker, args=(i + 1,), daemon=True)
        threads.append(t)
        t.start()

    # 等待结束
    for t in threads:
        t.join(timeout=MAX_WAIT_MINUTES * 60 + 60)

    if success_event.is_set() and success_result[0]:
        order_data = success_result[0]
        tid, pay_amount, ts = success_meta[0]
        pay_url = order_data.get("payUrl") or order_data.get("url") or order_data.get("signUrl")
        save_result("success", "下单成功（%d检测+%d下单线程）" % (DETECT_THREADS, ORDER_THREADS),
                    price=pay_amount, detect_time=ts, pay_url=pay_url, order_data=order_data)
    elif timeout_event.is_set():
        log("⏰ 等待超时（%d 分钟），退出" % MAX_WAIT_MINUTES)
        save_result("timeout", "等待 %d 分钟后仍未补货" % MAX_WAIT_MINUTES)
    else:
        log("❌ 所有线程均已退出，未能成功下单")
        save_result("failed", "下单未成功（%d检测+%d下单线程）" % (DETECT_THREADS, ORDER_THREADS))


if __name__ == "__main__":
    main()

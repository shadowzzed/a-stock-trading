#!/usr/bin/env python3
"""
交易工具集自检命令

用法: python -m tools.doctor

退出码: 0=全部通过, 1=存在 ERROR
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import get_config

_errors = 0


def check(name, check_fn, level="warn"):
    """运行单个检查项

    Args:
        name: 检查项名称
        check_fn: 返回 (ok: bool, msg: str) 的函数
        level: "error" = 失败时为 ERROR, "warn" = 失败时为 WARN（可选功能）
    """
    global _errors
    try:
        ok, msg = check_fn()
    except Exception as e:
        ok, msg = False, str(e)

    if ok:
        print("  [OK]    %s" % name)
        if msg:
            print("          %s" % msg)
    elif level == "error":
        _errors += 1
        print("  [ERROR] %s" % name)
        if msg:
            print("          %s" % msg)
    else:
        print("  [WARN]  %s" % name)
        if msg:
            print("          %s" % msg)
    return ok


def main():
    global _errors
    print("a-stock-trading 自检")
    print("=" * 50)

    cfg = get_config()

    # ── 1. 配置加载 ──
    check("配置加载", lambda: (True, "data_root: %s" % cfg["data_root"]))

    # ── 2. 目录是否存在 ──
    check("data_root 目录", lambda: (os.path.isdir(cfg["data_root"]), cfg["data_root"]), "error")
    check("daily 目录", lambda: (os.path.isdir(cfg["daily_dir"]), cfg["daily_dir"]))
    check("intraday 目录", lambda: (os.path.isdir(cfg["intraday_dir"]), cfg["intraday_dir"]))
    check("logs 目录", lambda: (os.path.isdir(cfg["logs_dir"]), cfg["logs_dir"]))

    # ── 3. 关键文件（ERROR 级别）──
    check("stocks.md 股票池", lambda: (os.path.isfile(cfg["stocks_file"]), cfg["stocks_file"]), "error")
    check("intraday.db 行情库", lambda: (os.path.isfile(cfg["intraday_db"]), cfg["intraday_db"]), "error")

    # ── 4. 路径可写（ERROR 级别）──
    def check_writable():
        test_path = os.path.join(cfg["data_root"], ".doctor_test")
        try:
            with open(test_path, "w") as f:
                f.write("test")
            os.remove(test_path)
            return True, ""
        except Exception as e:
            return False, str(e)
    check("data_root 可写", check_writable, "error")

    # ── 5. AI 配置（至少一个，否则 ERROR）──
    has_grok = bool(cfg["grok_api_key"])
    has_deepseek = bool(cfg["ai_api_key"])
    check("AI 提供商（至少一个）",
          lambda: (has_grok or has_deepseek,
                   "Grok: %s, DeepSeek: %s" % (
                       "已配置" if has_grok else "未配置",
                       "已配置" if has_deepseek else "未配置")),
          "error")

    # ── 6. 飞书配置（可选）──
    has_feishu = bool(cfg["feishu_app_id"] and cfg["feishu_app_secret"])
    check("飞书 Bot", lambda: (has_feishu, "已配置" if has_feishu else "未配置（可选）"))
    has_webhook = bool(cfg["feishu_webhook_url"])
    check("飞书 Webhook", lambda: (has_webhook, "已配置" if has_webhook else "未配置（可选）"))

    # ── 7. 外部集成（可选）──
    check("TrendRadar 输出目录", lambda: (os.path.isdir(cfg["trendradar_output"]), cfg["trendradar_output"]))

    # ── 8. 核心依赖可导入（ERROR 级别）──
    def check_import(mod):
        try:
            __import__(mod)
            return True, ""
        except ImportError as e:
            return False, str(e)

    check("mootdx 可导入", lambda: check_import("mootdx"), "error")
    check("pandas 可导入", lambda: check_import("pandas"), "error")
    check("langgraph 可导入", lambda: check_import("langgraph"), "error")
    check("langchain_openai 可导入", lambda: check_import("langchain_openai"), "error")

    # ── 汇总 ──
    print("=" * 50)
    if _errors:
        print("自检完成: %d 个 ERROR" % _errors)
        sys.exit(1)
    else:
        print("自检完成: 全部通过")


if __name__ == "__main__":
    main()

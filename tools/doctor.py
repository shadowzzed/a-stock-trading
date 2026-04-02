#!/usr/bin/env python3
"""
交易工具集自检命令

用法: python -m tools.doctor
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import get_config


def check(name, check_fn):
    """运行单个检查项"""
    try:
        ok, msg = check_fn()
    except Exception as e:
        ok, msg = False, str(e)

    status = "OK" if ok else "WARN" if msg else "ERROR"
    # WARN = ok but with caveat, ERROR = not ok
    if ok:
        print("  [OK]   %s" % name)
        if msg:
            print("         %s" % msg)
    else:
        print("  [WARN] %s" % name)
        if msg:
            print("         %s" % msg)
    return ok


def main():
    print("a-stock-trading 自检")
    print("=" * 40)

    cfg = get_config()

    # 1. 配置加载
    check("配置加载", lambda: (True, "data_root: %s" % cfg["data_root"]))

    # 2. 路径是否存在
    check("data_root 目录", lambda: (os.path.isdir(cfg["data_root"]), cfg["data_root"]))
    check("daily 目录", lambda: (os.path.isdir(cfg["daily_dir"]), cfg["daily_dir"]))
    check("intraday 目录", lambda: (os.path.isdir(cfg["intraday_dir"]), cfg["intraday_dir"]))
    check("logs 目录", lambda: (os.path.isdir(cfg["logs_dir"]), cfg["logs_dir"]))

    # 3. 关键文件
    check("stocks.md", lambda: (os.path.isfile(cfg["stocks_file"]), cfg["stocks_file"]))
    check("intraday.db", lambda: (os.path.isfile(cfg["intraday_db"]), cfg["intraday_db"]))

    # 4. 路径可写
    def check_writable():
        test_path = os.path.join(cfg["data_root"], ".doctor_test")
        try:
            with open(test_path, "w") as f:
                f.write("test")
            os.remove(test_path)
            return True, ""
        except Exception as e:
            return False, str(e)
    check("data_root 可写", check_writable)

    # 5. AI 配置
    check("AI Grok (XAI_API_KEY)", lambda: (bool(cfg["grok_api_key"]), "已配置" if cfg["grok_api_key"] else "未配置"))
    check("AI DeepSeek (ARK_API_KEY)", lambda: (bool(cfg["ai_api_key"]), "已配置" if cfg["ai_api_key"] else "未配置"))

    # 6. 飞书配置
    has_feishu = bool(cfg["feishu_app_id"] and cfg["feishu_app_secret"])
    check("飞书 Bot", lambda: (has_feishu, "已配置" if has_feishu else "未配置（可选）"))

    has_webhook = bool(cfg["feishu_webhook_url"])
    check("飞书 Webhook", lambda: (has_webhook, "已配置" if has_webhook else "未配置（可选）"))

    # 7. 外部集成
    check("TrendRadar 输出目录", lambda: (os.path.isdir(cfg["trendradar_output"]), cfg["trendradar_output"]))

    # 8. 模块导入
    def check_import(mod):
        try:
            __import__(mod)
            return True, ""
        except ImportError as e:
            return False, str(e)

    check("mootdx 可导入", lambda: check_import("mootdx"))
    check("pandas 可导入", lambda: check_import("pandas"))
    check("langgraph 可导入", lambda: check_import("langgraph"))
    check("langchain_openai 可导入", lambda: check_import("langchain_openai"))

    print("=" * 40)
    print("自检完成")


if __name__ == "__main__":
    main()

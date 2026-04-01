"""
全局配置加载器

优先级：环境变量 > config.yaml > 默认值

数据目录结构：
    {data_root}/
    ├── intraday/intraday.db
    ├── daily/YYYY-MM-DD/
    ├── news_monitor.db
    ├── stocks.md
    └── logs/
"""

import os
import shutil

_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
_config_cache = None


def _load_yaml_config():
    """加载 config.yaml"""
    config_path = os.path.join(_PROJECT_ROOT, "config.yaml")
    if not os.path.exists(config_path):
        return {}
    try:
        # 简单解析，不依赖 pyyaml
        config = {}
        with open(config_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if ":" in line:
                    key, value = line.split(":", 1)
                    key = key.strip()
                    value = value.strip().strip("'\"")
                    config[key] = value
        return config
    except Exception:
        return {}


def get_config():
    """获取配置（带缓存）"""
    global _config_cache
    if _config_cache is not None:
        return _config_cache

    yaml_cfg = _load_yaml_config()

    # data_root: 环境变量 > config.yaml > 默认值
    data_root = os.environ.get(
        "TRADING_DATA_ROOT",
        yaml_cfg.get("data_root", os.path.join(_PROJECT_ROOT, "runtime_data"))
    )
    data_root = os.path.expanduser(data_root)

    _config_cache = {
        "project_root": _PROJECT_ROOT,
        "data_root": data_root,
        "daily_dir": os.path.join(data_root, "daily"),
        "intraday_db": os.path.join(data_root, "intraday", "intraday.db"),
        "intraday_dir": os.path.join(data_root, "intraday"),
        "news_db": os.path.join(data_root, "news_monitor.db"),
        "stocks_file": os.path.join(data_root, "stocks.md"),
        "logs_dir": os.path.join(data_root, "logs"),

        # AI 配置（环境变量 > config.yaml > 默认值）
        "ai_api_key": os.environ.get("ARK_API_KEY", yaml_cfg.get("ai_api_key", "")),
        "ai_api_base": os.environ.get("ARK_API_BASE", yaml_cfg.get("ai_api_base", "https://ark.cn-beijing.volces.com/api/v3")),
        "ai_model": os.environ.get("ARK_MODEL", yaml_cfg.get("ai_model", "")),
    }
    return _config_cache


def init_data_dirs():
    """初始化数据目录结构，首次运行时自动调用"""
    cfg = get_config()
    data_root = cfg["data_root"]

    dirs = [
        data_root,
        cfg["daily_dir"],
        cfg["intraday_dir"],
        cfg["logs_dir"],
    ]
    for d in dirs:
        os.makedirs(d, exist_ok=True)

    # 首次运行：从仓库模板复制 stocks.md
    stocks_dest = cfg["stocks_file"]
    stocks_template = os.path.join(_PROJECT_ROOT, "knowledge", "stocks_template.md")
    if not os.path.exists(stocks_dest) and os.path.exists(stocks_template):
        shutil.copy2(stocks_template, stocks_dest)
        print("[init] stocks.md 已从模板复制到 %s" % stocks_dest)

    print("[init] 数据目录: %s" % data_root)
    return cfg


def reload_config():
    """强制重新加载配置"""
    global _config_cache
    _config_cache = None
    return get_config()

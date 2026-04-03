"""
全局配置加载器 — 唯一配置入口

优先级：环境变量 > config.yaml > 默认值

所有业务模块必须通过 get_config() 获取路径，禁止自行拼接或 hard code。

数据目录结构：
    {data_root}/
    ├── daily/YYYY-MM-DD/
    ├── intraday/intraday.db
    ├── memory/main/              ← 记忆目录（跨周期复盘记忆）
    ├── knowledge/                ← 知识库目录（交易规则、量化参数）
    │   ├── quantitative_rules.json
    │   └── 框架.md
    ├── agent_lessons.json        ← 经验库文件（回测验证的教训）
    ├── news_monitor.db
    ├── stocks.md
    ├── logs/
    ├── integrations/trendradar/
    └── state/news_monitor/
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

    # data_root: 环境变量 > config.yaml > 默认值（项目内 runtime_data/）
    data_root = os.environ.get(
        "TRADING_DATA_ROOT",
        yaml_cfg.get("data_root", os.path.join(_PROJECT_ROOT, "runtime_data"))
    )
    data_root = os.path.expanduser(data_root)

    # trendradar_output: 环境变量 > config.yaml > 默认值
    trendradar_output = os.environ.get(
        "TRENDRADAR_OUTPUT",
        yaml_cfg.get("trendradar_output", os.path.join(data_root, "integrations", "trendradar"))
    )
    trendradar_output = os.path.expanduser(trendradar_output)

    # news_state_dir: 环境变量 > config.yaml > 默认值
    news_state_dir = os.environ.get(
        "NEWS_STATE_DIR",
        yaml_cfg.get("news_state_dir", os.path.join(data_root, "state", "news_monitor"))
    )
    news_state_dir = os.path.expanduser(news_state_dir)

    _config_cache = {
        "project_root": _PROJECT_ROOT,
        "data_root": data_root,
        "daily_dir": os.path.join(data_root, "daily"),
        "intraday_db": os.path.join(data_root, "intraday", "intraday.db"),
        "intraday_dir": os.path.join(data_root, "intraday"),
        "news_db": os.path.join(data_root, "news_monitor.db"),
        "stocks_file": os.path.join(data_root, "stocks.md"),
        "logs_dir": os.path.join(data_root, "logs"),
        "backtest_dir": os.path.join(data_root, "backtest"),
        "memory_dir": os.path.join(data_root, "memory", "main"),
        "knowledge_dir": os.path.join(_PROJECT_ROOT, "knowledge"),
        "lessons_file": os.path.join(data_root, "agent_lessons.json"),
        "trendradar_output": trendradar_output,
        "news_state_dir": news_state_dir,

        # AI 配置 — DeepSeek（环境变量 > config.yaml > 默认值）
        "ai_api_key": os.environ.get("ARK_API_KEY", yaml_cfg.get("ai_api_key", "")),
        "ai_api_base": os.environ.get("ARK_API_BASE", yaml_cfg.get("ai_api_base", "https://ark.cn-beijing.volces.com/api/v3")),
        "ai_model": os.environ.get("ARK_MODEL", yaml_cfg.get("ai_model", "")),

        # AI 配置 — Grok（xAI，主力 AI）
        "grok_api_key": os.environ.get("XAI_API_KEY", yaml_cfg.get("grok_api_key", "")),
        "grok_api_base": yaml_cfg.get("grok_api_base", "https://api.x.ai/v1"),
        "grok_model": yaml_cfg.get("grok_model", "grok-3-fast"),

        # AI 配置 — GLM（智谱，Anthropic 协议）
        "glm_api_key": os.environ.get("GLM_API_KEY", yaml_cfg.get("glm_api_key", "")),
        "glm_api_base": yaml_cfg.get("glm_api_base", "https://open.bigmodel.cn/api/anthropic"),
        "glm_model": yaml_cfg.get("glm_model", "glm-5"),

        # 飞书配置（环境变量 > config.yaml > 默认值）
        "feishu_app_id": os.environ.get("FEISHU_APP_ID", yaml_cfg.get("feishu_app_id", "")),
        "feishu_app_secret": os.environ.get("FEISHU_APP_SECRET", yaml_cfg.get("feishu_app_secret", "")),
        "feishu_receive_id": os.environ.get("FEISHU_RECEIVE_ID", yaml_cfg.get("feishu_receive_id", "")),
        "feishu_webhook_url": os.environ.get("FEISHU_WEBHOOK_URL", yaml_cfg.get("feishu_webhook_url", "")),

        # 交易对话 Bot（独立飞书应用）
        "chat_feishu_app_id": os.environ.get("CHAT_FEISHU_APP_ID", yaml_cfg.get("chat_feishu_app_id", "")),
        "chat_feishu_app_secret": os.environ.get("CHAT_FEISHU_APP_SECRET", yaml_cfg.get("chat_feishu_app_secret", "")),
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
        cfg["backtest_dir"],
        cfg["memory_dir"],
        cfg["knowledge_dir"],
        cfg["trendradar_output"],
        cfg["news_state_dir"],
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


def get_ai_providers():
    """返回 AI 提供商列表（按优先级：GLM > Grok > DeepSeek）"""
    cfg = get_config()
    providers = []
    if cfg["glm_api_key"]:
        providers.append({
            "name": "GLM",
            "protocol": "anthropic",
            "base": cfg["glm_api_base"],
            "key": cfg["glm_api_key"],
            "model": cfg["glm_model"],
        })
    if cfg["grok_api_key"]:
        providers.append({
            "name": "Grok",
            "protocol": "openai",
            "base": cfg["grok_api_base"],
            "key": cfg["grok_api_key"],
            "model": cfg["grok_model"],
        })
    if cfg["ai_api_key"]:
        providers.append({
            "name": "DeepSeek",
            "protocol": "openai",
            "base": cfg["ai_api_base"],
            "key": cfg["ai_api_key"],
            "model": cfg["ai_model"],
        })
    return providers


def reload_config():
    """强制重新加载配置"""
    global _config_cache
    _config_cache = None
    return get_config()

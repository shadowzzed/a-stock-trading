import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import YAML from 'yaml';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PROJECT_ROOT = path.resolve(__dirname, '..');

const CONFIG_PATH = path.join(PROJECT_ROOT, 'config.yaml');
const ENV_PREFIX = 'CHAT_';

let _config = null;

function loadConfig() {
  if (_config) return _config;

  const cfg = {};

  // Load YAML
  if (fs.existsSync(CONFIG_PATH)) {
    const raw = fs.readFileSync(CONFIG_PATH, 'utf8');
    const parsed = YAML.parse(raw);
    if (parsed) Object.assign(cfg, parsed);
  }

  // Env overrides (CHAT_FEISHU_APP_ID → chat_feishu_app_id)
  for (const [key, val] of Object.entries(process.env)) {
    if (key.startsWith(ENV_PREFIX)) {
      const yamlKey = key.slice(ENV_PREFIX.length).toLowerCase();
      if (val) cfg[yamlKey] = val;
    }
  }

  // Expand ~ in paths
  if (cfg.data_root) cfg.data_root = expandHome(cfg.data_root);

  _config = cfg;
  return cfg;
}

function expandHome(p) {
  if (p.startsWith('~')) return path.join(process.env.HOME, p.slice(1));
  return p;
}

export function getFeishuConfig() {
  const cfg = loadConfig();
  return {
    appId: cfg.chat_feishu_app_id || cfg.feishu_app_id || '',
    appSecret: cfg.chat_feishu_app_secret || cfg.feishu_app_secret || '',
  };
}

export function getDataDir() {
  const cfg = loadConfig();
  return cfg.data_root || path.join(process.env.HOME, 'shared/trading');
}

export function getAIProviders() {
  const cfg = loadConfig();
  const providers = [];

  // Grok (primary)
  if (cfg.grok_api_key) {
    providers.push({
      name: 'Grok',
      base: cfg.grok_api_base || 'https://api.x.ai/v1',
      key: cfg.grok_api_key,
      model: cfg.grok_model || 'grok-3-fast',
    });
  }

  // DeepSeek via Volcengine (fallback)
  if (cfg.ai_api_key) {
    providers.push({
      name: 'DeepSeek',
      base: cfg.ai_api_base || 'https://ark.cn-beijing.volces.com/api/v3',
      key: cfg.ai_api_key,
      model: cfg.ai_model || 'ep-20260211173256-z9vg4',
    });
  }

  return providers;
}

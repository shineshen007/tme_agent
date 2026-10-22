"""Non-secret settings and endpoint-bound keys, with opt-in OS vault retrieval."""
from __future__ import annotations

import copy
import json
import os
import re
import threading
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

PRESETS = {
    "deepseek": {"base_url": "https://api.deepseek.com", "model": "deepseek-flash", "auth_mode": "bearer", "api_key_env": "DEEPSEEK_API_KEY"},
    "openai_compatible": {"base_url": "https://api.openai.com/v1", "model": "", "auth_mode": "bearer", "api_key_env": "OPENAI_API_KEY"},
    "ollama": {"base_url": "http://localhost:11434/v1", "model": "", "auth_mode": "none", "api_key_env": "OLLAMA_API_KEY"},
    "lm_studio": {"base_url": "http://localhost:1234/v1", "model": "", "auth_mode": "none", "api_key_env": "LM_STUDIO_API_KEY"},
}
FIELDS = {"provider", "base_url", "model", "auth_mode", "api_key_env", "json_mode", "timeout_seconds", "max_tokens", "temperature", "max_tokens_field"}
WORKER_KEY = "MULTIOMICS_MODEL_API_KEY"
_keys = {}
_env_bindings = {}
_known_secrets = set()
_lock = threading.RLock()


def endpoint(value):
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("接口地址必须是 http/https 基础地址，不能含密钥、查询参数或完整 chat/completions 路径。")
    if parts.path.rstrip('/').endswith('/chat/completions'):
        raise ValueError("请填写基础地址（通常以 /v1 结尾），不要填写 /chat/completions。")
    # Accessing port validates malformed port syntax too.
    port = parts.port
    host = parts.hostname.lower()
    if ':' in host:
        host = '[' + host + ']'
    default_port = 443 if parts.scheme == 'https' else 80
    authority = host + (f':{port}' if port and port != default_port else '')
    return urlunsplit((parts.scheme, authority, parts.path.rstrip('/'), '', ''))


def normalize(settings):
    value = copy.deepcopy(settings)
    provider = value.get('provider', 'deepseek')
    if provider not in PRESETS:
        raise ValueError("请选择 DeepSeek、OpenAI compatible、Ollama 或 LM Studio。")
    value['provider'] = provider
    for name, default in PRESETS[provider].items():
        value.setdefault(name, default)
    for name in ('base_url', 'model', 'api_key_env'):
        if not isinstance(value[name], str) or not value[name].strip():
            raise ValueError(f"请填写 {name}。")
        value[name] = value[name].strip()
    value['base_url'] = endpoint(value['base_url'])
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', value['api_key_env']):
        raise ValueError("密钥环境变量名格式不正确。")
    if value.setdefault('auth_mode', 'bearer') not in {'bearer', 'none'}:
        raise ValueError("认证方式必须是 bearer 或 none。")
    if value.setdefault('json_mode', 'json_object') not in {'json_object', 'prompt'}:
        raise ValueError("JSON 模式必须是 json_object 或 prompt。")
    if value.setdefault('max_tokens_field', 'max_tokens') not in {'max_tokens', 'max_completion_tokens'}:
        raise ValueError("输出长度字段不受支持。")
    for name, default, lower, upper in [('timeout_seconds', 120, 1, 3600), ('max_tokens', 32000, 1, 1000000)]:
        number = value.setdefault(name, default)
        if isinstance(number, bool) or not isinstance(number, int) or not lower <= number <= upper:
            raise ValueError(f"{name} 必须是 {lower}–{upper} 的整数。")
    temperature = value.setdefault('temperature', 0)
    if temperature is not None and (isinstance(temperature, bool) or not isinstance(temperature, (int, float)) or not 0 <= temperature <= 2):
        raise ValueError("temperature 必须为空（不发送）或 0–2。")
    return value


def overlay(base, override):
    if not isinstance(override, dict) or set(override) - FIELDS:
        raise ValueError("api.local.yaml 仅接受模型连接设置；密钥请使用 /key 输入。")
    result = copy.deepcopy(base)
    # A preset change must not inherit another provider's address or key name.
    if override.get('provider', base.get('provider', 'deepseek')) != base.get('provider', 'deepseek'):
        provider = override['provider']
        if provider not in PRESETS:
            raise ValueError('Unknown model provider')
        result.update(PRESETS[provider])
        result.pop('thinking', None)
    result.update(override)
    return normalize(result)


def public_settings(cfg):
    value = normalize(cfg['llm'])
    return {name: value[name] for name in sorted(FIELDS)}


def save_settings(root, settings):
    import yaml
    value = normalize(settings)
    path = Path(root) / 'config/api.local.yaml'
    temp = path.with_suffix('.yaml.tmp')
    temp.write_text(yaml.safe_dump({'llm': {name: value[name] for name in sorted(FIELDS)}}, allow_unicode=True, sort_keys=False), encoding='utf-8')
    temp.replace(path)
    return value


def credential(cfg):
    llm = cfg.get('llm', {})
    if llm.get('auth_mode') == 'none':
        return None
    name = llm.get('api_key_env') or 'DEEPSEEK_API_KEY'
    address = endpoint(llm['base_url']) if llm.get('base_url') else 'legacy'
    from .credentials import enabled, read
    if enabled(cfg):
        with _lock:
            if (address, name) in _keys:
                return _keys[address, name]
        # Only trusted UI/controller code consults the vault. Worker snapshots
        # disable persistence and receive their endpoint's key explicitly.
        key = None
        if cfg.get('_credential_lookup_address') != address:
            cfg['_credential_lookup_address'] = address
            try:
                key = read(cfg)
                cfg['_credential_error'] = None
            except RuntimeError as exc:
                cfg['_credential_error'] = str(exc)
                return None
        if key:
            store_key(cfg, key)
            return key
        # External environment keys remain endpoint-bound, but aren't saved.
    with _lock:
        if not os.environ.get(name):
            # Explicit environment removal revokes credentials for this name.
            for stored in list(_keys):
                if stored[1] == name:
                    del _keys[stored]
            _env_bindings.pop(name, None)
            return None
        if (address, name) in _keys:
            return _keys[address, name]
        key = os.environ.get(name)
        if key:
            if name not in _env_bindings or _env_bindings[name][1] != key:
                _env_bindings[name] = (address, key)
            if _env_bindings[name] == (address, key):
                _known_secrets.add(key)
                return key
    return None


def store_key(cfg, key):
    key = (key or '').strip()
    if not key:
        return
    name = cfg.get('llm', {}).get('api_key_env') or 'DEEPSEEK_API_KEY'
    address = endpoint(cfg['llm']['base_url']) if cfg['llm'].get('base_url') else 'legacy'
    with _lock:
        _keys[address, name] = key
        _known_secrets.add(key)
        # Preserve the existing environment-based API for legacy callers.
        os.environ[name] = key
        _env_bindings[name] = (address, key)


def ready(cfg):
    return cfg.get('llm', {}).get('auth_mode') == 'none' or bool(credential(cfg))


def forget_key(cfg):
    from .credentials import enabled, delete
    if enabled(cfg):
        delete(cfg)
    name = cfg['llm'].get('api_key_env') or 'DEEPSEEK_API_KEY'
    address = endpoint(cfg['llm']['base_url'])
    with _lock:
        _keys.pop((address, name), None)
        bound = _env_bindings.get(name)
        if bound and bound[0] == address:
            _env_bindings.pop(name, None)
            os.environ.pop(name, None)
    cfg['_credential_error'] = None
    cfg['_credential_lookup_address'] = address


def redact(value):
    text = str(value)
    with _lock:
        secrets = _known_secrets | set(_keys.values())
    # 环境值只有长度像真实凭据时才参与替换：CI 运行器会注入 ACTIONS_* 等短字符串，
    # 仅按名字匹配会把普通提示词误判为泄漏。真实模型密钥均远长于 16 字符。
    secrets |= {item for name, item in os.environ.items()
                if item and len(item) >= 16
                and re.search(r'(?:KEY|TOKEN|PASSWORD|SECRET)', name, re.I)}
    for secret in sorted(secrets, key=len, reverse=True):
        if len(secret) >= 4:
            text = text.replace(secret, '[REDACTED]')
    return text


def worker_environment(cfg):
    key = credential(cfg)
    env = {}
    if key:
        env[WORKER_KEY] = key
    return env


def task_environment():
    return {name: value for name, value in os.environ.items()
            if not re.search(r'(?:KEY|TOKEN|PASSWORD|SECRET)', name, re.I)
            and name not in _env_bindings and value not in _known_secrets
            and name != 'MULTIOMICS_MODEL_CONFIG'}

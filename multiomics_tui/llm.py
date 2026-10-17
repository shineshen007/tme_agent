"""TUI LLM plumbing; v5 UI keys use the OS vault, workers use memory only."""
from __future__ import annotations

import os
import uuid
from pathlib import Path

DEFAULT_ENV = "DEEPSEEK_API_KEY"


def load_tui_config(root, snapshot=None):
    """Allow endpoint/model overrides without editing the distributed study file."""
    import yaml
    from orchestrator.io import load_config
    cfg = load_config(root)
    if snapshot is not None:
        import json
        from .model_config import WORKER_KEY
        cfg['llm'] = json.loads(Path(snapshot).read_text(encoding='utf-8'))
        cfg['llm']['api_key_env'] = WORKER_KEY
        cfg['distribution'] = {**cfg.get('distribution', {}), 'credential_storage': 'memory'}
        return cfg
    local = Path(root) / "config/api.local.yaml"
    if local.is_file():
        data = yaml.safe_load(local.read_text(encoding="utf-8")) or {}
        override = data.get("llm") or {}
        from .model_config import overlay
        cfg["llm"] = overlay(cfg["llm"], override)
    return cfg


def llm_config(cfg) -> dict:
    return (cfg or {}).get("llm") or {}


def key_env_name(cfg) -> str:
    return llm_config(cfg).get("api_key_env") or DEFAULT_ENV


def key_present(cfg) -> bool:
    from .model_config import ready
    return ready(cfg)


def set_session_key(cfg, key: str) -> None:
    """Persist only when the distribution explicitly opts into the OS vault."""
    from .model_config import store_key
    from .credentials import enabled, write
    if enabled(cfg):
        # Commit to the vault before announcing success or retaining the key.
        write(cfg, key.strip())
        cfg['_credential_error'] = None
    store_key(cfg, key)


def build_client(root, cfg, registry_ids=None, audit_dir=None):
    """Reuse the audited project LLM client; audit/cache stay outside run dirs."""
    from .model_client import CompatibleClient
    root = Path(root)
    return CompatibleClient(
        root=root,
        run=root / "workspace/tui",
        cfg=cfg,
        transport=None,
        registry_ids=registry_ids,
        audit_dir=audit_dir or (root / "workspace/tui/llm_calls" / uuid.uuid4().hex),
    )

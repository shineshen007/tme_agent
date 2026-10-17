"""Installed application assets and user-owned workspace are separate roots."""
from __future__ import annotations

import json
import os
from pathlib import Path


def code_root(workspace):
    value = os.environ.get('MULTIOMICS_APP_ROOT')
    if value:
        path = Path(value).resolve(strict=True)
        if not (path / 'multiomics_tui/task_guard.py').is_file():
            raise RuntimeError('内置应用源码目录不完整。')
        return path
    return Path(workspace).resolve()


def resource(workspace, relative):
    return code_root(workspace) / relative


def bundled_runtime(workspace):
    marker = Path(workspace) / '.runtime/application.json'
    if not marker.is_file() or not os.environ.get('MULTIOMICS_APP_ROOT'):
        return None
    value = json.loads(marker.read_text(encoding='utf-8'))
    if Path(value['app_root']).resolve() != code_root(workspace):
        raise RuntimeError('工作目录与当前应用版本不匹配，请从应用入口重新启动。')
    return value

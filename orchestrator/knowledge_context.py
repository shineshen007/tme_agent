"""Workspace-scoped access to reviewed knowledge; no database is created by reads."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def shared_wiki_root(settings=None):
    settings = settings or {}
    if settings.get('shared_enabled', True) is False:
        return None
    if settings.get('shared_root'):
        return Path(settings['shared_root']).expanduser().resolve()
    if sys.platform == 'darwin':
        base = Path.home() / 'Library/Application Support'
    elif os.name == 'nt':
        base = Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local'))
    else:
        base = Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share'))
    return base / 'tme_agent/wiki'


def knowledge_service(root, settings=None, *, read_only=True):
    from .knowledge import KnowledgeService
    return KnowledgeService(Path(root) / 'knowledge/wiki',
                            shared_root=shared_wiki_root(settings), read_only=read_only)


def knowledge_snapshot(root, query, context=None, settings=None):
    return knowledge_service(root, settings).snapshot(query, context=context)

"""User-owned workspaces for ``/cd``.

A workspace owns configuration, inputs, tasks and results. Application code and
the installed runtime stay in the install directory; this module only creates
the files a new workspace needs and verifies that native restrictions still
apply there.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from native_dist.layout import code_root
from native_dist.public_release import safe_path


class WorkspaceError(ValueError):
    pass


# Destination -> ordered source candidates relative to the installation root.
# Missing optional sources (for example runtime.local.yaml in a source checkout)
# are skipped; a study configuration is the only required template.
TEMPLATES = {
    'config/study_config.yaml': ('config/study_config.example.yaml', 'config/study_config.yaml'),
    'config/runtime.local.yaml': ('config/runtime.local.yaml',),
    'config/api.local.yaml': ('config/api.local.yaml',),
    'data/raw/examples/synthetic_expression.csv': ('examples/synthetic_expression.csv',),
    'data/metadata/examples/synthetic_samples.csv': ('examples/synthetic_samples.csv',),
}
DIRECTORIES = ('config', 'data/raw', 'data/metadata', 'data/signature', 'knowledge/resources')
API_DEFAULT = ("# 使用界面 /model 设置服务和模型；密钥通过 /key 输入，不能写入本文件。\n"
               "llm: {}\n")


def is_workspace(path) -> bool:
    path = Path(path)
    return ((path / 'config/study_config.yaml').is_file()
            or (path / 'config/study_config.example.yaml').is_file())


def resolve_target(current_root, value) -> Path:
    """Resolve a shell-like directory argument against the current workspace."""
    text = str(value or '').strip()
    if not text:
        raise WorkspaceError('Usage: /cd <directory>')
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        text = text[1:-1]
    if not text:
        raise WorkspaceError('Usage: /cd <directory>')
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = Path(current_root) / path
    try:
        path = path.resolve(strict=True)
    except OSError as exc:
        raise WorkspaceError(f'Directory not found: {path}') from exc
    if not path.is_dir():
        raise WorkspaceError(f'Not a directory: {path}')
    return path


def _copy_exclusive(source: Path, destination: Path) -> bool:
    if destination.exists():
        return False
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise WorkspaceError(f'Cannot create {destination.parent}: {exc}') from exc
    try:
        with source.open('rb') as src, destination.open('xb') as dst:
            while True:
                block = src.read(1024 * 1024)
                if not block:
                    break
                dst.write(block)
        return True
    except FileExistsError:
        return False
    except OSError as exc:
        raise WorkspaceError(f'Cannot write {destination}: {exc}') from exc


def initialize(path, template_root) -> list[str]:
    """Create missing workspace files without replacing anything.

    The directory must already exist. Only paths that do not exist are written,
    and every destination is checked for symlinked ancestors first.
    """
    path = Path(path).resolve()
    template_root = Path(template_root).resolve()
    created: list[str] = []
    for relative in DIRECTORIES:
        destination = safe_path(path, relative)
        if destination.exists():
            if not destination.is_dir():
                raise WorkspaceError(f'Not a directory: {destination}')
            continue
        try:
            destination.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise WorkspaceError(f'Cannot create {destination}: {exc}') from exc
    study = safe_path(path, 'config/study_config.yaml')
    if not study.exists():
        source = None
        for candidate in TEMPLATES['config/study_config.yaml']:
            try:
                candidate_path = safe_path(template_root, candidate)
            except ValueError:
                continue
            if candidate_path.is_file():
                source = candidate_path
                break
        if source is None:
            raise WorkspaceError(
                f'No study configuration template was found in {template_root}')
        if _copy_exclusive(source, study):
            created.append('config/study_config.yaml')
    for relative, candidates in TEMPLATES.items():
        destination = safe_path(path, relative)
        if destination.exists():
            continue
        source = None
        for candidate in candidates:
            try:
                candidate_path = safe_path(template_root, candidate)
            except ValueError:
                continue
            if candidate_path.is_file():
                source = candidate_path
                break
        if source is None and relative == 'config/api.local.yaml':
            try:
                with destination.open('x', encoding='utf-8') as stream:
                    stream.write(API_DEFAULT)
                created.append(relative)
            except FileExistsError:
                pass
            except OSError as exc:
                raise WorkspaceError(f'Cannot write {destination}: {exc}') from exc
            continue
        if source is not None and _copy_exclusive(source, destination):
            created.append(relative)
    # Built-in datasets are data, not templates: they are seeded last so a
    # manifest problem never leaves a workspace half-configured. Seeding is
    # exclusive, so an existing user copy is preserved and never replaced.
    created.extend(seed_datasets(path))
    return created


def seed_datasets(path) -> list[str]:
    """Copy built-in dataset files into ``data/``; return the relative paths written.

    A source checkout without ``datasets/`` seeds nothing. The manifest is
    validated before the first copy, so a corrupt dataset fails the whole
    workspace check instead of writing a partial copy.
    """
    from .datasets import DatasetError, seeds
    try:
        mapping = seeds(path)
    except DatasetError as exc:
        raise WorkspaceError(f'Built-in dataset check failed: {exc}') from exc
    written: list[str] = []
    for relative, source in sorted(mapping.items()):
        destination = safe_path(path, relative)
        try:
            if _copy_exclusive(Path(source), destination):
                written.append(relative)
        except OSError as exc:
            raise WorkspaceError(f'Cannot seed dataset file {relative}: {exc}') from exc
    return written


def ensure_probe(path, *, timeout=900) -> dict | None:
    """Ensure a native probe record belongs to this workspace.

    Linux applies Landlock directly and treats a missing record as allowed, so
    ``require_probe`` returns without work. macOS and Windows require a record
    bound to the workspace's configuration and libraries; create one with the
    installed doctor when it is missing or stale.
    """
    from native_dist import doctor
    try:
        doctor.require_probe(Path(path))
        return None
    except Exception:
        if sys.platform == 'linux':
            return None
    code = code_root(Path(path))
    env = dict(os.environ)
    env['PYTHONUTF8'] = '1'
    env.pop('PYTHONHOME', None)
    env.pop('PYTHONSTARTUP', None)
    python_path = str(code)
    if env.get('PYTHONPATH'):
        python_path += os.pathsep + env['PYTHONPATH']
    env['PYTHONPATH'] = python_path
    try:
        result = subprocess.run(
            [sys.executable, '-B', '-m', 'native_dist.doctor', '--project-root', str(path)],
            cwd=str(path), env=env, capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise WorkspaceError(f'Workspace check could not start: {exc}') from exc
    if result.returncode:
        from multiomics_tui.model_config import redact
        detail = (result.stderr or result.stdout or '').strip().splitlines()
        message = detail[-1] if detail else f'exit code {result.returncode}'
        raise WorkspaceError('Workspace check failed: ' + redact(message))
    try:
        doctor.require_probe(Path(path))
    except Exception as exc:  # noqa: BLE001 - report the probe record, not a silent pass
        raise WorkspaceError(f'Workspace check did not produce a valid record: {exc}') from exc
    return None

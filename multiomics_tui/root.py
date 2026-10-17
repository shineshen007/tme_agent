"""Project-root resolution for the CLI/TUI entry.

The launcher always passes ``--project-root``; interactive use without it walks up
from the current directory and finally falls back to this package's parent.
"""
from __future__ import annotations

import os
from pathlib import Path

MARKER = Path("config/study_config.yaml")


class ProjectRootError(RuntimeError):
    pass


def _is_root(path: Path) -> bool:
    return (path / MARKER).is_file() or (path / "config/study_config.example.yaml").is_file()


def resolve_root(explicit=None) -> Path:
    candidates = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    env = os.environ.get("MULTIOMICS_ROOT")
    if env:
        candidates.append(Path(env).expanduser())
    cwd = Path.cwd()
    candidates.append(cwd)
    candidates.extend(cwd.resolve().parents)
    candidates.append(Path(__file__).resolve().parents[1])

    seen = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if _is_root(resolved):
            return resolved
    raise ProjectRootError(
        f"Project root not found (missing {MARKER}). Run from the project directory "
        f"or set MULTIOMICS_ROOT / pass --project-root."
    )


def venv_python(root) -> Path:
    from .platform_support import python_path
    path = python_path(Path(root))
    if not path.is_file():
        raise ProjectRootError(f"Virtual environment missing at {path}; run the setup launcher first")
    return path

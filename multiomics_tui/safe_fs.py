"""Descriptor-relative file creation for trusted parent writes.

Generated task code may rewrite anything inside its own task directory, including
replacing a subdirectory with a symbolic link. A trusted parent that later opens
``task_dir/reports/failure.txt`` by path would follow that link and write outside
the sandbox. ``open_beneath`` walks every component relative to an already opened
directory descriptor with ``O_NOFOLLOW``/``O_DIRECTORY`` and therefore refuses a
link at any level without a check-to-open race.

POSIX is the enforced path. Windows has no ``dir_fd`` in ``os.open``; there the
function resolves the candidate, rejects symbolic links component by component
and verifies containment. That fallback is best effort and is reported as such;
Windows native isolation is covered separately by the AppContainer acceptance.
"""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath


def open_beneath(base: Path | str, relative: str, flags: int, mode: int = 0o600) -> int:
    """Open ``relative`` under ``base`` without following any symbolic link.

    Returns an owned file descriptor. The caller closes it (for example with
    ``os.fdopen``). ``relative`` must be a project-relative POSIX path.
    """
    parts = PurePosixPath(str(relative)).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("Unsafe relative path for trusted write: " + repr(relative))
    base = Path(base)

    if os.name == "nt":
        candidate = base.joinpath(*parts)
        resolved_base = base.resolve(strict=True)
        cursor = base
        for part in parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise OSError(f"Refusing to follow a symbolic link in {cursor}")
        resolved = candidate.resolve(strict=False)
        if not resolved.is_relative_to(resolved_base):
            raise OSError(f"Refusing to write outside {resolved_base}: {candidate}")
        return os.open(candidate, flags, mode)

    directory_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    close_fd = getattr(os, "O_CLOEXEC", 0)
    dir_fd = os.open(base, directory_flags | close_fd)
    try:
        for part in parts[:-1]:
            child = os.open(part, directory_flags | close_fd, dir_fd=dir_fd)
            os.close(dir_fd)
            dir_fd = child
        return os.open(parts[-1], flags | nofollow | close_fd, mode, dir_fd=dir_fd)
    finally:
        os.close(dir_fd)

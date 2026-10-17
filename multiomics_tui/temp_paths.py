"""R requires a space-free TMPDIR. Alias only the task's own temporary folder."""
from __future__ import annotations
import json
import os
from pathlib import Path
import tempfile
import uuid


def _short_windows_path(path):
    import ctypes
    from ctypes import wintypes
    function = ctypes.WinDLL("kernel32", use_last_error=True).GetShortPathNameW
    function.argtypes, function.restype = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD], wintypes.DWORD
    size = function(str(path), None, 0)
    if not size:
        return None
    buffer = ctypes.create_unicode_buffer(size)
    if not function(str(path), buffer, size):
        return None
    return Path(buffer.value) if " " not in buffer.value else None


def task_tmp(root, task_dir):
    target = Path(task_dir) / "tmp"
    target.mkdir(parents=True, exist_ok=True)
    if " " not in str(target):
        return target
    if os.name == "nt":
        short = _short_windows_path(target)
        if short:
            return short
        raise RuntimeError("R 需要无空格临时路径，本机未提供 Windows 短路径。请将测试包解压到 C:\\MultiomicsTest 等当前用户可写且无空格的位置后重新配置。")
    base = Path(tempfile.gettempdir())
    if " " in str(base):
        base = Path("/tmp")
    # Private parent prevents other users from replacing the alias. Only the
    # trusted controller creates/removes it; the sandbox cannot mutate the link.
    parent = Path(tempfile.mkdtemp(prefix="multiomics_rtmp_", dir=base))
    alias = parent / "tmp"
    alias.symlink_to(target.resolve(), target_is_directory=True)
    state_dir = Path(root) / ".runtime/rtmp_aliases"
    state_dir.mkdir(parents=True, exist_ok=True)
    state = {"parent": str(parent), "alias": str(alias), "target": str(target.resolve())}
    (state_dir / (Path(task_dir).name + ".json")).write_text(json.dumps(state), encoding="utf-8")
    return alias


def cleanup(root, task_id, task_dir=None):
    state = Path(root) / ".runtime/rtmp_aliases" / (task_id + ".json")
    if not state.is_file():
        return
    data = json.loads(state.read_text(encoding="utf-8"))
    alias, parent = Path(data["alias"]), Path(data["parent"])
    expected = ((Path(task_dir) if task_dir is not None else Path(root) / "analysis" / task_id) / "tmp").resolve()
    if (alias.parent != parent or not parent.name.startswith("multiomics_rtmp_") or
            alias.name != "tmp" or not alias.is_symlink() or alias.resolve() != expected):
        raise ValueError("R temporary alias identity mismatch; no cleanup performed")
    alias.unlink()  # Never recursively delete the task or follow its link.
    parent.rmdir()
    state.unlink()

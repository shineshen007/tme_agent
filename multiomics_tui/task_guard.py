"""Fail-closed host guard for untrusted generated analysis scripts.

Landlock keeps project/raw files read-only. libseccomp blocks new sockets and
metadata changes that Landlock does not cover. Restrictions survive exec/fork.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import errno
import json
import os
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "multiomics_tui"

LIBC = ctypes.CDLL(None, use_errno=True) if sys.platform == "linux" else None
PR_SET_NO_NEW_PRIVS = 38
LANDLOCK_RULE_PATH_BENEATH = 1
LANDLOCK_CREATE_RULESET_VERSION = 1
WRITE_RIGHTS = sum(1 << n for n in (1, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14))
READ_RIGHTS = (1 << 0) | (1 << 2) | (1 << 3)
NET_RIGHTS = 3  # bind/connect TCP, supported from ABI 4


class Ruleset(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64),
                ("handled_access_net", ctypes.c_uint64)]


class PathRule(ctypes.Structure):
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int)]


def _syscall(number, *args):
    value = LIBC.syscall(number, *args)
    if value < 0:
        raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))
    return value


def available() -> None:
    if sys.platform == "darwin":
        from .macos_guard import available as mac_available
        return mac_available()
    if os.name == "nt":
        from .windows_native import _api
        _api()
        return
    if sys.platform != "linux" or os.uname().machine != "x86_64":
        raise RuntimeError("Host guard requires Linux x86_64")
    abi = _syscall(444, 0, 0, LANDLOCK_CREATE_RULESET_VERSION)
    if abi < 4:
        raise RuntimeError("Landlock ABI >=4 is required")
    if not ctypes.util.find_library("seccomp"):
        raise RuntimeError("libseccomp is required")


def restrict(task_dir: Path) -> None:
    available()
    path = Path(task_dir).resolve(strict=True)
    if not path.is_dir():
        raise ValueError("Task directory is missing")
    if LIBC.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "PR_SET_NO_NEW_PRIVS failed")
    attr = Ruleset(WRITE_RIGHTS | READ_RIGHTS, NET_RIGHTS)
    ruleset_fd = _syscall(444, ctypes.byref(attr), ctypes.sizeof(attr), 0)
    def allow(target: Path, rights: int) -> None:
        target_fd = os.open(target, os.O_PATH | os.O_CLOEXEC)
        try:
            rule = PathRule(rights, target_fd)
            _syscall(445, ruleset_fd, LANDLOCK_RULE_PATH_BENEATH, ctypes.byref(rule), 0)
        finally:
            os.close(target_fd)
    try:
        allow(path, WRITE_RIGHTS | READ_RIGHTS)
        allow(Path("/dev/null"), (1 << 1) | (1 << 2))
        for name in ("/usr", "/lib", "/lib64", "/opt/conda", "/etc", "/dev/urandom",
                     "/dev/random", "/proc/self"):
            candidate = Path(name)
            if candidate.exists():
                allow(candidate, READ_RIGHTS if candidate.is_dir() else ((1 << 0) | (1 << 2)))
        runtime = Path(sys.prefix).resolve()
        if runtime.is_dir() and not runtime.is_relative_to(path):
            allow(runtime, READ_RIGHTS)
        allowed = json.loads(os.environ.get("TASK_ALLOWED_READS", "[]"))
        if not isinstance(allowed, list) or len(allowed) > 32:
            raise ValueError("Invalid task read allowlist")
        for name in allowed:
            candidate = Path(name).resolve(strict=True)
            if not candidate.is_absolute():
                raise ValueError("Task read path is not absolute")
            allow(candidate, READ_RIGHTS if candidate.is_dir() else ((1 << 0) | (1 << 2)))
        _syscall(446, ruleset_fd, 0)
    finally:
        os.close(ruleset_fd)
    library = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    library.seccomp_init.argtypes = [ctypes.c_uint32]
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    library.seccomp_syscall_resolve_name.restype = ctypes.c_int
    library.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    library.seccomp_rule_add.restype = ctypes.c_int
    library.seccomp_load.argtypes = [ctypes.c_void_p]
    library.seccomp_load.restype = ctypes.c_int
    library.seccomp_release.argtypes = [ctypes.c_void_p]
    ctx = library.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW
    if not ctx:
        raise RuntimeError("seccomp_init failed")
    try:
        deny = 0x00050000 | errno.EPERM  # SCMP_ACT_ERRNO(EPERM)
        required = ("socket", "connect", "bind", "sendto", "sendmsg", "ptrace",
                    "process_vm_readv", "process_vm_writev", "pidfd_getfd", "io_uring_setup", "kill", "tgkill", "tkill")
        optional = ("listen", "accept", "accept4", "recvfrom", "recvmsg",
                    "chmod", "fchmod", "fchmodat", "chown", "fchown", "lchown",
                    "fchownat", "utime", "utimes", "utimensat", "futimesat",
                    "setxattr", "lsetxattr", "fsetxattr", "removexattr",
                    "lremovexattr", "fremovexattr")
        for name in (*required, *optional):
            number = library.seccomp_syscall_resolve_name(name.encode())
            if number < 0:
                if name in required:
                    raise RuntimeError(f"Required seccomp syscall is unavailable: {name}")
                continue
            result = library.seccomp_rule_add(ctx, deny, number, 0)
            if result != 0:
                raise OSError(-result, f"seccomp rule failed for {name}")
        result = library.seccomp_load(ctx)
        if result != 0:
            raise OSError(-result, "seccomp_load failed")
    finally:
        library.seccomp_release(ctx)


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 3 or args[1] != "--":
        raise SystemExit("Usage: python -m multiomics_tui.task_guard TASK_DIR -- PROGRAM [ARGS...]")
    if sys.platform == "darwin":
        from multiomics_tui.macos_guard import execute
        execute(Path(args[0]), args[2:])
        return 1
    if os.name == "nt":
        from multiomics_tui.windows_native import run_appcontainer
        return run_appcontainer(Path(args[0]), args[2:], dict(os.environ))
    restrict(Path(args[0]))
    os.execvpe(args[2], args[2:], os.environ)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

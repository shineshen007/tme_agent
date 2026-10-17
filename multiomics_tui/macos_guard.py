"""Native Seatbelt launcher and non-reaping waitid using the Darwin ABI.

Constants follow Apple XNU bsd/sys/wait.h and bsd/sys/signal.h.
"""
from __future__ import annotations
import ctypes
import json
import os
import stat
import subprocess
from pathlib import Path
import sys


def available():
    if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file():
        raise RuntimeError("macOS native task sandbox is unavailable; no unrestricted fallback")


# macOS startup needs the root directory itself (getcwd/volume queries).
# A literal grant does not authorize reads of files beneath it. Keep sysctl,
# network, credential services and non-task writes restricted.
ROOT_DIRECTORY_READ = '(allow file-read-data file-read-metadata (literal "/"))'

SYSTEM_PATHS = ["/System", "/usr/lib", "/usr/share", "/usr/bin", "/bin", "/private/etc",
                "/Library/Frameworks/Python.framework", "/Library/Frameworks/R.framework",
                "/private/var/db/dyld"]

# macOS 26 evaluates uname() as an OID-form sysctl read/write; without a
# resolvable write rule the process aborts inside dyld before any user code.
# Binding the write rule to a name that never exists keeps every real write
# (e.g. sethostname) denied while letting uname succeed. kern.usrstack64 is
# needed for R's stack-limit query; kern.hostname is needed for uname().
SYSCTL_READ = ('(allow sysctl-read (sysctl-name-prefix "hw.") '
               '(sysctl-name "kern.osrelease" "kern.ostype" "kern.osversion" "kern.osproductversion" '
               '"kern.version" "kern.hostname" "kern.usrstack64" "kern.maxfilesperproc" "kern.boottime" '
               '"sysctl.name2oid" "machdep.cpu.brand_string"))')
SYSCTL_WRITE_NOOP = '(allow sysctl-write (sysctl-name "multiomics.uname-write-noop"))'

# Credential isolation needs the keychain services; the access control on the
# stored item still denies generated task code (verified by the doctor probe).
MACH_LOOKUP = ('(allow mach-lookup (global-name "com.apple.cfprefsd.agent") '
               '(global-name "com.apple.cfprefsd.daemon") (global-name "com.apple.FontObjectsServer") '
               '(global-name "com.apple.securityd") (global-name "com.apple.SecurityServer") '
               '(global-name "com.apple.trustd") (global-name "com.apple.trustd.agent"))')

# R resolves its shell through this Apple resolver symlink; resolve() would
# only grant the link target, not the link itself or its parent metadata.
SELECT_SH = "/private/var/select/sh"

_external_library_cache: list[str] | None = None


def linked_external_library_dirs() -> list[str]:
    """Directories holding external dylibs linked by the selected interpreter.

    Homebrew-built interpreters link extension modules (for example _ssl,
    _sqlite3, _lzma, _hashlib, _zstd, _decimal) against other formulae under the
    Homebrew prefix. A deny-default Seatbelt profile must authorize those exact
    directories or the extension fails to dlopen. Discovered with otool so only
    genuinely linked directories are granted, on any Homebrew prefix.
    """
    global _external_library_cache
    if _external_library_cache is not None:
        return _external_library_cache
    prefixes = ("/usr/local/", "/opt/homebrew/")
    roots = [Path(sys.executable)]
    base = Path(sys.base_prefix)
    for pattern in ("lib/python*/lib-dynload", "lib/libpython*"):
        roots.extend(sorted(base.glob(pattern)))
    directories: set[str] = set()
    for root in roots:
        if root.is_dir():
            targets = [p for p in sorted(root.rglob("*.so"))]
        elif root.is_file():
            targets = [root]
        else:
            continue
        for target in targets[:600]:
            try:
                output = subprocess.run(["otool", "-L", str(target)], capture_output=True,
                                        text=True, encoding="utf-8", errors="replace",
                                        timeout=10, check=False).stdout
            except (OSError, subprocess.SubprocessError):
                continue
            for line in output.splitlines()[1:]:
                dependency = line.strip().split(" ")[0]
                if dependency.startswith(prefixes):
                    directories.add(str(Path(dependency).parent))
    _external_library_cache = sorted(directories)
    return _external_library_cache


def runtime_path(name: str) -> dict:
    """Snapshot only controller-selected runtimes, including every symlink hop.

    Seatbelt checks metadata of lexical aliases before exec/dyld. Resolving just
    the final target drops Homebrew opt and Rscript aliases from the policy.
    Ancestors get literal metadata permission, never recursive data permission.
    """
    original = Path(name)
    if not original.is_absolute() or ".." in original.parts:
        raise ValueError("Runtime path must be absolute without parent traversal")
    metadata, links = set(), []
    hops = 0

    def walk(path):
        nonlocal hops
        cursor = Path(path.anchor)
        for part in path.parts[1:]:
            if part == "..":
                cursor = cursor.parent
                continue
            cursor = cursor / part
            info = cursor.lstat()  # Dangling aliases fail closed.
            metadata.add(str(cursor))
            if stat.S_ISLNK(info.st_mode):
                hops += 1
                if hops > 40:
                    raise ValueError("Runtime symlink loop or excessive hops: " + str(original))
                target = os.readlink(cursor)
                links.append(dict(path=str(cursor), target=target, device=info.st_dev, inode=info.st_ino))
                cursor = walk(Path(target) if os.path.isabs(target) else cursor.parent / target)
        return cursor

    target = walk(original)
    # Include the spelling presented to exec/dyld, even when a parent is aliased.
    metadata.update(str(p) for p in [original, *original.parents] if str(p) != "/")
    return dict(source=str(original), target=str(target), metadata=sorted(metadata), links=links)


def snapshot_runtimes(paths: list[str]) -> list[dict]:
    return [runtime_path(name) for name in sorted(set(paths))]


def validate_runtimes(snapshots: list[dict]) -> None:
    if not isinstance(snapshots, list) or len(snapshots) > 40:
        raise ValueError("Invalid runtime path snapshots")
    for item in snapshots:
        if not isinstance(item, dict) or runtime_path(item.get("source", "")) != item:
            raise ValueError("Runtime path changed since submission; run setup again")


def profile(task_dir: Path, reads: list[str], runtimes: list[str] | None = None,
            snapshots: list[dict] | None = None) -> str:
    task = task_dir.resolve(strict=True)
    rules = ['(version 1)', '(deny default)', '(deny network*)', ROOT_DIRECTORY_READ,
             '(allow process-exec process-fork)', '(allow ipc-posix-shm)', '(deny process-info*)',
             '(deny sysctl*)', SYSCTL_WRITE_NOOP,
             '(allow process-info-pidinfo process-codesigning-status* (target self))',
             SYSCTL_READ, MACH_LOOKUP]
    own = [p for p in [*SYSTEM_PATHS, *linked_external_library_dirs(), sys.base_prefix, sys.prefix]
           if Path(p).exists()]
    mappings = snapshot_runtimes(own) + (snapshots if snapshots is not None else
                                         snapshot_runtimes(runtimes or []))
    validate_runtimes(mappings)
    targets = [item['target'] for item in mappings]
    for name in sorted(set([*targets, *reads])):
        path = Path(name).resolve(strict=True) if Path(name).exists() else None
        if path:
            kind = "subpath" if path.is_dir() else "literal"
            rules.append(f'(allow file-read* ({kind} {json.dumps(str(path), ensure_ascii=False)}))')
    # A literal metadata grant permits alias traversal, not reading its siblings.
    for name in sorted({p for item in mappings for p in item['metadata']}):
        rules.append(f'(allow file-read-metadata (literal {json.dumps(name, ensure_ascii=False)}))')
    # Do not map executable pages from approved input files or the task directory.
    for name in sorted(set(targets)):
        path = Path(name)
        kind = 'subpath' if path.is_dir() else 'literal'
        rules.append(f'(allow file-map-executable ({kind} {json.dumps(str(path), ensure_ascii=False)}))')
    rules.append(f'(allow file-read* file-write* (subpath {json.dumps(str(task), ensure_ascii=False)}))')
    # realpath()/normalizePath(mustWork=TRUE) canonicalizes a path by stat-ing
    # every ancestor directory. Grant literal metadata (never recursive data)
    # for the task directory's ancestors so generated code can resolve paths.
    for name in sorted({str(p) for p in task.parents if str(p) != "/"}):
        rules.append(f'(allow file-read-metadata (literal {json.dumps(name, ensure_ascii=False)}))')
    for device in ("/dev/null", "/dev/urandom", "/dev/random"):
        rules.append(f'(allow file-read* (literal "{device}"))')
    rules.append(f'(allow file-read* (literal "{SELECT_SH}"))')
    rules.append('(allow file-write-data (literal "/dev/null"))')
    return "\n".join(rules)


def execution_policy(task_dir: Path, argv: list[str], env: dict) -> tuple[str, list[dict]]:
    reads = json.loads(env.get("TASK_ALLOWED_READS", "[]"))
    runtimes = json.loads(env.get('TASK_RUNTIME_READS', '[]'))
    if not all(isinstance(values, list) and len(values) <= 32 and
               all(isinstance(x, str) for x in values) for values in (reads, runtimes)):
        raise ValueError("Invalid task/runtime read allowlist")
    snapshots = json.loads(env.get('TASK_RUNTIME_PATHS', '[]'))
    validate_runtimes(snapshots)
    if not argv or not any(Path(item['target']).is_file() and
                           argv[0] in (item['source'], item['target']) for item in snapshots):
        raise ValueError("Executable absent from trusted runtime snapshot")
    policy = profile(task_dir, reads, runtimes, snapshots)
    tmp = Path(env.get("TMPDIR", str(task_dir / "tmp")))
    if tmp.is_symlink() and tmp.resolve() == (task_dir / "tmp").resolve():
        policy += '\n(allow file-read* (literal ' + json.dumps(str(tmp), ensure_ascii=False) + '))'
        # R's space-free TMPDIR alias needs the same literal traversal metadata,
        # but its writable target must never become an executable runtime.
        for name in runtime_path(str(tmp))['metadata']:
            policy += '\n(allow file-read-metadata (literal ' + json.dumps(name, ensure_ascii=False) + '))'
    return policy, snapshots


def execute(task_dir: Path, argv: list[str]):
    available()
    policy, snapshots = execution_policy(task_dir, argv, os.environ)
    # Recheck immediately before exec so changed aliases never reuse stale grants.
    validate_runtimes(snapshots)
    os.execv("/usr/bin/sandbox-exec", ["sandbox-exec", "-p", policy, *argv])


class SigInfo(ctypes.Structure):
    _fields_ = [("signo", ctypes.c_int), ("errno", ctypes.c_int), ("code", ctypes.c_int),
                ("pid", ctypes.c_int), ("uid", ctypes.c_uint), ("status", ctypes.c_int),
                ("tail", ctypes.c_byte * 128)]


def peek_exit(pid):
    libc = ctypes.CDLL(None, use_errno=True)
    waitid = libc.waitid
    waitid.argtypes = [ctypes.c_int, ctypes.c_uint, ctypes.POINTER(SigInfo), ctypes.c_int]
    waitid.restype = ctypes.c_int
    info = SigInfo()
    if waitid(1, pid, ctypes.byref(info), 0x4 | 0x1 | 0x20) != 0:
        raise OSError(ctypes.get_errno(), "Darwin waitid failed")
    return (info.status if info.code == 1 else -info.status) if info.pid else None

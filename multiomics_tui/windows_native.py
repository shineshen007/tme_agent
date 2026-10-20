"""Windows native Job Objects and AppContainer launcher (no network capability).

All ACL grants are confined to installation-owned runtime copies, task input
snapshots and the task output directory. Never grant access to original inputs.
"""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
import hashlib
import os
from pathlib import Path
import subprocess


class STARTUPINFO(C.Structure):
    _fields_ = [("cb", W.DWORD), ("reserved", W.LPWSTR), ("desktop", W.LPWSTR),
                ("title", W.LPWSTR), ("x", W.DWORD), ("y", W.DWORD),
                ("xsize", W.DWORD), ("ysize", W.DWORD), ("xchars", W.DWORD),
                ("ychars", W.DWORD), ("fill", W.DWORD), ("flags", W.DWORD),
                ("show", W.WORD), ("reserved2size", W.WORD), ("reserved2", C.c_void_p),
                ("stdin", W.HANDLE), ("stdout", W.HANDLE), ("stderr", W.HANDLE)]


class STARTUPINFOEX(C.Structure):
    _fields_ = [("startup", STARTUPINFO), ("attributes", C.c_void_p)]


class PROCESSINFO(C.Structure):
    _fields_ = [("process", W.HANDLE), ("thread", W.HANDLE),
                ("pid", W.DWORD), ("tid", W.DWORD)]


class SECURITY_CAPABILITIES(C.Structure):
    _fields_ = [("sid", C.c_void_p), ("capabilities", C.c_void_p),
                ("count", W.DWORD), ("reserved", W.DWORD)]


class BASIC_LIMIT(C.Structure):
    _fields_ = [("process_time", C.c_int64), ("job_time", C.c_int64),
                ("flags", W.DWORD), ("min_ws", C.c_size_t), ("max_ws", C.c_size_t),
                ("active", W.DWORD), ("affinity", C.c_size_t),
                ("priority", W.DWORD), ("scheduling", W.DWORD)]


class IO_COUNTERS(C.Structure):
    _fields_ = [(name, C.c_uint64) for name in ("reads", "writes", "other", "read_bytes", "write_bytes", "other_bytes")]


class EXTENDED_LIMIT(C.Structure):
    _fields_ = [("basic", BASIC_LIMIT), ("io", IO_COUNTERS),
                ("process_memory", C.c_size_t), ("job_memory", C.c_size_t),
                ("peak_process", C.c_size_t), ("peak_job", C.c_size_t)]


class BASIC_ACCOUNTING(C.Structure):
    _fields_ = [("user_time", C.c_int64), ("kernel_time", C.c_int64),
                ("user_period", C.c_int64), ("kernel_period", C.c_int64),
                ("page_faults", W.DWORD), ("total", W.DWORD),
                ("active", W.DWORD), ("terminated", W.DWORD)]


def _api():
    if os.name != "nt":
        raise RuntimeError("Windows native backend is available only on Windows")
    k = C.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateJobObjectW": ([C.c_void_p, W.LPCWSTR], W.HANDLE),
        "SetInformationJobObject": ([W.HANDLE, C.c_int, C.c_void_p, W.DWORD], W.BOOL),
        "QueryInformationJobObject": ([W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.c_void_p], W.BOOL),
        "AssignProcessToJobObject": ([W.HANDLE, W.HANDLE], W.BOOL),
        "TerminateJobObject": ([W.HANDLE, W.UINT], W.BOOL),
        "CloseHandle": ([W.HANDLE], W.BOOL),
        "InitializeProcThreadAttributeList": ([C.c_void_p, W.DWORD, W.DWORD, C.POINTER(C.c_size_t)], W.BOOL),
        "UpdateProcThreadAttribute": ([C.c_void_p, W.DWORD, C.c_size_t, C.c_void_p, C.c_size_t, C.c_void_p, C.c_void_p], W.BOOL),
        "DeleteProcThreadAttributeList": ([C.c_void_p], None),
        "CreateProcessW": ([W.LPCWSTR, W.LPWSTR, C.c_void_p, C.c_void_p, W.BOOL, W.DWORD, C.c_void_p, W.LPCWSTR, C.c_void_p, C.POINTER(PROCESSINFO)], W.BOOL),
        "ResumeThread": ([W.HANDLE], W.DWORD),
        "WaitForSingleObject": ([W.HANDLE, W.DWORD], W.DWORD),
        "GetExitCodeProcess": ([W.HANDLE, C.POINTER(W.DWORD)], W.BOOL),
        "TerminateProcess": ([W.HANDLE, W.UINT], W.BOOL),
        "GetCurrentProcess": ([], W.HANDLE),
        "DuplicateHandle": ([W.HANDLE, W.HANDLE, W.HANDLE, C.POINTER(W.HANDLE), W.DWORD, W.BOOL, W.DWORD], W.BOOL),
        "CreateFileW": ([W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p, W.DWORD, W.DWORD, W.HANDLE], W.HANDLE),
        "GetStdHandle": ([W.DWORD], W.HANDLE),
        "SetHandleInformation": ([W.HANDLE, W.DWORD, W.DWORD], W.BOOL),
        "LocalFree": ([C.c_void_p], C.c_void_p),
    }
    for name, (args, result) in signatures.items():
        function = getattr(k, name)
        function.argtypes, function.restype = args, result
    return k


def _check(ok, operation):
    if not ok:
        raise OSError(C.get_last_error(), f"{operation} failed")


class WindowsProcess:
    def __init__(self, api, info):
        self.api, self.info, self.pid = api, info, info.pid
        self.returncode = None

    def poll(self):
        if self.returncode is None and self.api.WaitForSingleObject(self.info.process, 0) == 0:
            code = W.DWORD()
            _check(self.api.GetExitCodeProcess(self.info.process, C.byref(code)), "GetExitCodeProcess")
            self.returncode = code.value
        return self.returncode

    def wait(self):
        _check(self.api.WaitForSingleObject(self.info.process, 0xFFFFFFFF) == 0, "WaitForSingleObject")
        return self.poll()

    def close(self):
        self.api.CloseHandle(self.info.process)


class WindowsJob:
    def __init__(self):
        self.api = _api()
        self.handle = self.api.CreateJobObjectW(None, None)
        _check(self.handle, "CreateJobObject")
        limits = EXTENDED_LIMIT()
        limits.basic.flags = 0x2000  # KILL_ON_JOB_CLOSE; no breakaway allowed.
        _check(self.api.SetInformationJobObject(self.handle, 9, C.byref(limits), C.sizeof(limits)), "SetInformationJobObject")

    def launch(self, argv, cwd, env=None, attributes=None):
        # Suspend until assigned, so descendants cannot escape before assignment.
        owned_attributes = None
        handles = []
        info = PROCESSINFO()
        try:
            if attributes is None:
                size = C.c_size_t()
                self.api.InitializeProcThreadAttributeList(None, 1, 0, C.byref(size))
                owned_attributes = C.create_string_buffer(size.value)
                _check(self.api.InitializeProcThreadAttributeList(owned_attributes, 1, 0, C.byref(size)), 'InitializeProcThreadAttributeList')
                attributes = owned_attributes
            current = self.api.GetCurrentProcess()
            for index, number in enumerate((-10, -11, -12)):
                source = self.api.GetStdHandle(number & 0xffffffff)
                temporary = None
                if not source or source == C.c_void_p(-1).value:
                    temporary = self.api.CreateFileW('NUL', 0x80000000 if index == 0 else 0x40000000,
                                                     3, None, 3, 0x80, None)
                    _check(temporary and temporary != C.c_void_p(-1).value, 'CreateFileW(NUL)')
                    source = temporary
                duplicate = W.HANDLE()
                try:
                    _check(self.api.DuplicateHandle(current, source, current, C.byref(duplicate), 0, True, 2), 'DuplicateHandle')
                    handles.append(duplicate.value)
                finally:
                    if temporary:
                        self.api.CloseHandle(temporary)
            handle_list = (W.HANDLE * len(handles))(*handles)
            _check(self.api.UpdateProcThreadAttribute(attributes, 0, 0x20002, handle_list,
                                                       C.sizeof(handle_list), None, None), 'Handle inheritance allowlist')
            si = STARTUPINFOEX()
            si.startup.cb = C.sizeof(si)
            si.attributes = C.cast(attributes, C.c_void_p)
            si.startup.flags = 0x100
            si.startup.stdin, si.startup.stdout, si.startup.stderr = handles
            command = C.create_unicode_buffer(subprocess.list2cmdline(list(map(str, argv))))
            env_block = None
            if env is not None:
                env_block = C.create_unicode_buffer("\0".join(f"{k}={v}" for k, v in sorted(env.items(), key=lambda p:p[0].upper())) + "\0\0")
            flags = 0x4 | 0x400 | 0x8000000 | 0x80000
            _check(self.api.CreateProcessW(str(argv[0]), command, None, None, True, flags,
                                          env_block, str(cwd), C.byref(si), C.byref(info)), 'CreateProcessW')
            try:
                _check(self.api.AssignProcessToJobObject(self.handle, info.process), 'AssignProcessToJobObject')
                _check(self.api.ResumeThread(info.thread) != 0xffffffff, 'ResumeThread')
            except BaseException:
                self.api.TerminateProcess(info.process, 1)
                self.api.CloseHandle(info.process)
                raise
            finally:
                self.api.CloseHandle(info.thread)
            return WindowsProcess(self.api, info)
        finally:
            for handle in handles:
                self.api.CloseHandle(handle)
            if owned_attributes is not None:
                self.api.DeleteProcThreadAttributeList(owned_attributes)

    def alive(self):
        accounting = BASIC_ACCOUNTING()
        _check(self.api.QueryInformationJobObject(self.handle, 1, C.byref(accounting), C.sizeof(accounting), None), "QueryInformationJobObject")
        return bool(accounting.active)

    def terminate(self):
        _check(self.api.TerminateJobObject(self.handle, 1), "TerminateJobObject")

    def close(self):
        self.api.CloseHandle(self.handle)


def _grant(path: Path, sid: str, rights: str, directory: bool):
    if path.is_symlink():
        raise ValueError(f"Symlink cannot be granted to AppContainer: {path}")
    permission = f"*{sid}:{'(OI)(CI)' if directory else ''}({rights})"
    result = subprocess.run(["icacls", str(path), "/grant:r", permission, "/q", *(["/t"] if directory else [])],
                            capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError(f"Cannot configure private AppContainer path {path}: {result.stderr or result.stdout}")


def _profile_root(profile_dir: Path, profiles_base: Path, name: str) -> Path:
    relative = profile_dir.resolve(strict=True).relative_to(profiles_base.resolve(strict=True))
    # GetAppContainerFolderPath normally returns Packages/<name>/AC, not
    # Packages/<name>. Restrict the entire newly owned profile tree.
    if not relative.parts or relative.parts[0].lower() != name.lower():
        raise ValueError("Unexpected AppContainer profile identity")
    return profiles_base.resolve() / relative.parts[0]


def launch_paths(task_dir, env):
    """The workspace is explicit: inspections need not live under analysis/."""
    raw = env.get('MULTIOMICS_WORKSPACE_ROOT')
    if not raw:
        raise RuntimeError('Trusted workspace root is missing; rerun setup')
    root = Path(raw).resolve(strict=True)
    task = Path(task_dir).resolve(strict=True)
    if not task.is_relative_to(root) or task == root:
        raise ValueError('Task directory is outside the workspace')
    relative = task.relative_to(root)
    if relative.parts[0] not in {'analysis', '.runtime'}:
        raise ValueError('Unexpected task directory')
    # A /cd workspace owns data and results; the expensive private Python/R
    # runtime stays read-only in the installation and is shared by workspaces.
    runtime_root = Path(env.get('MULTIOMICS_RUNTIME_ROOT', raw)).resolve(strict=True)
    runtime = runtime_root / '.runtime/sandbox_runtime'
    inputs_base = root / '.runtime/inputs'
    view = Path(env.get('PROJECT_ROOT', '')).resolve(strict=True)
    expected = inputs_base / task.name
    for value, boundary in ((runtime, runtime_root), (expected, root), (task, root)):
        for part in (value, *value.parents):
            if part == boundary.parent:
                break
            if part.is_symlink() or getattr(part, 'is_junction', lambda: False)():
                raise ValueError('Linked native workspace path is not permitted')
    if view != expected or not runtime.is_dir() or not view.is_dir():
        raise RuntimeError('Private native runtime or approved input snapshot is missing; rerun setup')
    return root, runtime, view


def profile_created(result):
    code = result & 0xffffffff
    if code == 0:
        return True
    if code == 0x800700b7:  # HRESULT_FROM_WIN32(ERROR_ALREADY_EXISTS)
        return False
    raise RuntimeError(f'CreateAppContainerProfile failed (HRESULT {code:#010x})')


def _remove_grant(path, sid):
    result = subprocess.run(['icacls', str(path), '/remove:g', '*' + sid, '/t', '/q'],
                            capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError('Cannot remove task AppContainer grant: ' + str(path))


def run_appcontainer(task_dir: Path, argv: list[str], env: dict) -> int:
    """Launch with explicit private reads and task-only writes; key-free environment."""
    import json
    k = _api()
    root, runtime, view = launch_paths(task_dir, env)
    # Even if a future caller supplies an unexpected environment, do not pass keys.
    allowed_env = {"PATH", "LANG", "LC_ALL", "TZ", "PROJECT_ROOT", "TASK_DIR", "TMPDIR", "TEMP", "TMP",
                   "HOME", "USERPROFILE", "PYTHONDONTWRITEBYTECODE", "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                   "MKL_NUM_THREADS", "SYSTEMROOT", "WINDIR", "R_LIBS_USER", "R_HOME", "PYTHONHOME"}
    clean_env = {name: value for name, value in env.items() if name in allowed_env}
    clean_env.update(HOME=str(task_dir), USERPROFILE=str(task_dir), PYTHONUTF8='1',
                     PYTHONIOENCODING='utf-8', XDG_CACHE_HOME=str(task_dir/'tmp/.cache'))
    private_python = runtime / "python/python.exe"
    private_r = next(iter(sorted((runtime / "r").glob("bin/**/Rscript.exe"))), None)
    interpreter = private_r if Path(argv[0]).name.lower().startswith("rscript") else private_python
    if interpreter is None or not interpreter.is_file():
        raise RuntimeError("Private native interpreter is missing; rerun setup")
    argv = [str(interpreter), *argv[1:]]
    if interpreter == private_r:
        clean_env["R_HOME"] = str(runtime / "r")
        clean_env["R_LIBS_USER"] = str(runtime / "r/library")
    else:
        clean_env["PYTHONHOME"] = str(runtime / "python")
    clean_env["PATH"] = os.pathsep.join([str(interpreter.parent), str(Path(os.environ["SYSTEMROOT"]) / "System32")])
    userenv = C.WinDLL("userenv", use_last_error=True)
    create = userenv.CreateAppContainerProfile
    create.argtypes = [W.LPCWSTR, W.LPCWSTR, W.LPCWSTR, C.c_void_p, W.DWORD, C.POINTER(C.c_void_p)]
    create.restype = C.c_long
    derive = userenv.DeriveAppContainerSidFromAppContainerName
    derive.argtypes, derive.restype = [W.LPCWSTR, C.POINTER(C.c_void_p)], C.c_long
    name = "multiomics." + hashlib.sha256(str(task_dir.resolve()).encode()).hexdigest()[:32]
    sid = C.c_void_p()
    result = create(name, name, "Restricted local analysis", None, 0, C.byref(sid))
    created = profile_created(result)
    if not created:
        result = derive(name, C.byref(sid))
        if result != 0:
            raise RuntimeError(f"AppContainer profile unavailable (HRESULT {result & 0xffffffff:#x})")
    adv = C.WinDLL("advapi32", use_last_error=True)
    adv.ConvertSidToStringSidW.argtypes = [C.c_void_p, C.POINTER(W.LPWSTR)]
    adv.ConvertSidToStringSidW.restype = W.BOOL
    adv.FreeSid.argtypes, adv.FreeSid.restype = [C.c_void_p], C.c_void_p
    sid_string = W.LPWSTR()
    size = C.c_size_t()
    attributes = None
    job, process = None, None
    granted = []
    try:
        _check(adv.ConvertSidToStringSidW(sid, C.byref(sid_string)), "ConvertSidToStringSid")
        # AppContainer normally owns a writable profile directory outside the
        # task. Deny that default write grant on this newly created profile.
        get_folder = userenv.GetAppContainerFolderPath
        get_folder.argtypes, get_folder.restype = [W.LPCWSTR, C.POINTER(W.LPWSTR)], C.c_long
        folder = W.LPWSTR()
        folder_result = get_folder(sid_string.value, C.byref(folder))
        if folder_result != 0:
            raise RuntimeError(f"GetAppContainerFolderPath failed (HRESULT {folder_result & 0xffffffff:#010x}); host user profile initialization failed")
        try:
            profile_dir = Path(folder.value).resolve(strict=True)
            allowed_profiles = Path(os.environ["LOCALAPPDATA"]) / "Packages"
            profile_root = _profile_root(profile_dir, allowed_profiles, name)
            restriction = f"*{sid_string.value}:(OI)(CI)(W,D,DC)"
            denied = subprocess.run(["icacls", str(profile_root), "/deny", restriction, "/t", "/q"],
                                    capture_output=True, text=True, check=False)
            if denied.returncode:
                raise RuntimeError("Cannot restrict AppContainer profile writes: " + denied.stderr + denied.stdout)
        finally:
            ole = C.WinDLL("ole32", use_last_error=True)
            ole.CoTaskMemFree.argtypes, ole.CoTaskMemFree.restype = [C.c_void_p], None
            ole.CoTaskMemFree(C.cast(folder, C.c_void_p))
        for target, rights in ((runtime, "RX"), (view, "RX"), (task_dir.resolve(), "M")):
            granted.append(target)
            _grant(target, sid_string.value, rights, True)
        # Low integrity may write only the task-owned directory. The input view
        # and private runtime stay read-only; no original data ACL is touched.
        result = subprocess.run(["icacls", str(task_dir), "/setintegritylevel", "(OI)(CI)L", "/t", "/q"],
                                capture_output=True, text=True, check=False)
        if result.returncode:
            raise RuntimeError("Cannot set task integrity label: " + result.stdout + result.stderr)
        k.InitializeProcThreadAttributeList(None, 2, 0, C.byref(size))
        attributes = C.create_string_buffer(size.value)
        _check(k.InitializeProcThreadAttributeList(attributes, 2, 0, C.byref(size)), "InitializeProcThreadAttributeList")
        capabilities = SECURITY_CAPABILITIES(sid, None, 0, 0)
        _check(k.UpdateProcThreadAttribute(attributes, 0, 0x20009, C.byref(capabilities), C.sizeof(capabilities), None, None), "UpdateProcThreadAttribute")
        job = WindowsJob()
        process = job.launch(argv, task_dir, clean_env, attributes)
        code = process.wait()
        # Descendants must not outlive the approved step.
        if job.alive():
            job.terminate()
        return code
    finally:
        if process:
            process.close()
        if job:
            job.close()
        if attributes:
            k.DeleteProcThreadAttributeList(attributes)
        cleanup_errors = []
        for target in reversed(granted):
            try:
                _remove_grant(target, sid_string.value)
            except Exception as exc:
                cleanup_errors.append(str(exc))
        if created:
            delete = userenv.DeleteAppContainerProfile
            delete.argtypes, delete.restype = [W.LPCWSTR], C.c_long
            result = delete(name)
            if result != 0:
                cleanup_errors.append(f'DeleteAppContainerProfile HRESULT {result & 0xffffffff:#010x}')
        if sid_string:
            k.LocalFree(C.cast(sid_string, C.c_void_p))
        adv.FreeSid(sid)
        if cleanup_errors:
            import sys
            # Preserve the primary exception; cleanup failures also fail a successful run.
            if sys.exc_info()[0] is not None:
                print('AppContainer cleanup: ' + '; '.join(cleanup_errors), file=sys.stderr)
            else:
                raise RuntimeError('; '.join(cleanup_errors))

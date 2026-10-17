"""Offline receiving-machine checks. Unknown errors never count as denials."""
from __future__ import annotations
import argparse
from contextlib import ExitStack
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import socket
import subprocess
import sys
import tempfile
import time


def guard_fingerprint(root):
    from .layout import resource
    h = hashlib.sha256()
    for area, names in {'multiomics_tui': ('task_guard.py', 'macos_guard.py', 'windows_native.py', 'task_runner.py', 'temp_paths.py'),
                        'native_dist': ('doctor.py', 'credential_probe.py', 'windows_runtime.py')}.items():
        for name in names:
            h.update(resource(root, area + '/' + name).read_bytes())
    return h.hexdigest()


def runtime_fingerprint(root):
    """Bind a passed record to the OS, selected interpreters, config and packages."""
    h = hashlib.sha256((platform.platform() + platform.machine() + sys.version).encode())
    from orchestrator.io import load_config
    from multiomics_tui.actions import rscript_path
    config = load_config(root).get('runtime', {})
    h.update(json.dumps(config, sort_keys=True).encode())
    paths = [Path(sys.executable).resolve(), Path(rscript_path(root)).resolve(),
             Path(root)/'.runtime/sandbox_runtime/runtime_manifest.json']
    private_py = Path(root)/'.runtime/sandbox_runtime/python/python.exe'
    if private_py.is_file():
        paths.append(private_py)
    for library in config.get('r_library_paths', []):
        paths.extend(sorted(Path(library).glob('*/DESCRIPTION')))
    for path in paths:
        h.update(str(path).encode())
        if path.is_file():
            h.update(path.read_bytes())
    from .environment import requirements
    from packaging.requirements import Requirement
    for spec in requirements(root):
        name = Requirement(spec).name
        try:
            version = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            version = 'missing'
        h.update((name + '=' + version).encode())
    return h.hexdigest()


def require_probe(root):
    if sys.platform == 'linux':
        return
    try:
        record = json.loads((root/'.runtime/doctor.json').read_text(encoding='utf-8'))
        if (record['status'] != 'passed' or record['platform'] != sys.platform or
            record['machine'] != platform.machine() or record['guard_fingerprint'] != guard_fingerprint(root) or
            record.get('runtime_fingerprint') != runtime_fingerprint(root)):
            raise ValueError('Probe record mismatch')
    except (OSError, ValueError, KeyError) as exc:
        raise RuntimeError('本机执行限制尚未通过行为验证。请用 start 重新检查；不会无保护运行。') from exc


def run_check(label, argv, diagnostics, **kwargs):
    from multiomics_tui.model_config import redact
    diagnostics['phase'] = label
    row = {'status':'running'}
    diagnostics.setdefault('stages', {})[label] = row
    started = time.monotonic()
    try:
        result = subprocess.run(list(map(str, argv)), capture_output=True, text=True,
                                encoding='utf-8', errors='replace', timeout=kwargs.pop('timeout', 90),
                                check=False, **kwargs)
        row.update(exit_code=result.returncode, stdout=redact(result.stdout[-2000:]),
                   stderr=redact(result.stderr[-4000:]))
        if result.returncode:
            raise RuntimeError(f'{label}: exit={result.returncode}; {row["stderr"]}')
        row['status'] = 'passed'
        return result
    except Exception as exc:
        row.update(status='failed', error_type=type(exc).__name__, error=redact(str(exc)))
        raise
    finally:
        row['seconds'] = round(time.monotonic()-started, 3)


def probe(root: Path, diagnostics: dict | None = None):
    from .layout import resource
    from orchestrator.io import load_config
    from multiomics_tui.task_guard import available
    from multiomics_tui.task_runner import _step_environment, file_hash
    from multiomics_tui.actions import rscript_path
    from multiomics_tui.temp_paths import cleanup
    available()
    root = Path(root).resolve()
    cfg = load_config(root)
    (root/'analysis').mkdir(exist_ok=True)
    (root/'.runtime').mkdir(exist_ok=True)
    diagnostics = diagnostics if diagnostics is not None else {}
    checks = diagnostics.setdefault('checks', {})
    diagnostics['phase'] = 'runtime_preparation'
    with tempfile.TemporaryDirectory(prefix='probe_source_', dir=root/'.runtime') as source_dir, \
         tempfile.TemporaryDirectory(prefix='task_native_probe_', dir=root/'analysis') as task_name, \
         socket.socket() as listener, ExitStack() as cleanups:
        source, task = Path(source_dir), Path(task_name)
        approved, outside = source/'approved.txt', source/'outside.txt'
        approved.write_text('approved', encoding='utf-8'); outside.write_text('protected', encoding='utf-8')
        (task/'tmp').mkdir()
        item = {'path':approved.relative_to(root).as_posix(), 'sha256':file_hash(approved)}
        cleanups.callback(cleanup, root, task.name)
        env = _step_environment(root, task, cfg, [item])
        listener.bind(('127.0.0.1', 0)); listener.listen(8)
        port = listener.getsockname()[1]
        prefix = [sys.executable, str(resource(root, 'multiomics_tui/task_guard.py')), str(task), '--']
        baseline = 'import pathlib,socket; p=pathlib.Path(' + repr(str(outside)) + '); assert p.read_text()=="protected"; p.open("a").close(); socket.create_connection(("127.0.0.1",' + str(port) + '),timeout=3).close();print("BASELINE_OK")'
        run_check('host_baseline', [sys.executable, '-c', baseline], diagnostics, cwd=root)
        if sys.platform == 'darwin':
            from multiomics_tui.macos_guard import execution_policy
            policy, mappings = execution_policy(task, [sys.executable], env)
            diagnostics['macos_python'] = {'policy':policy, 'runtime_paths':mappings}
        for label, command in [('python_startup', [sys.executable, '-c', 'print("STARTUP_OK")']),
                               ('r_startup', [rscript_path(root), '--vanilla', '-e', 'cat("STARTUP_OK")'])]:
            result = run_check(label, [*prefix, *command], diagnostics, env=env, cwd=task)
            if 'STARTUP_OK' not in result.stdout:
                raise RuntimeError(label + ': missing success marker')
        code = PYTHON_PROBE.replace('INPUT_REL', repr(item['path'])).replace('OUTSIDE', repr(str(outside))).replace('PORT', str(port)).replace('CONTROLLER_PID', str(os.getpid()))
        script = task/'probe.py'; script.write_text(code, encoding='utf-8')
        old_secret = os.environ.get('MULTIOMICS_PROBE_SECRET')
        os.environ['MULTIOMICS_PROBE_SECRET'] = 'synthetic-probe-only'
        try:
            run_check('python_permissions', [*prefix, sys.executable, str(script)], diagnostics, env=env, cwd=task)
        finally:
            if old_secret is None:
                os.environ.pop('MULTIOMICS_PROBE_SECRET', None)
            else:
                os.environ['MULTIOMICS_PROBE_SECRET'] = old_secret
        python_result = json.loads((task/'probe.json').read_text(encoding='utf-8'))
        checks.update(python_result['checks'])
        diagnostics['permission_errors'] = python_result['errors']
        checks['probe_original_unchanged'] = approved.read_text() == 'approved' and outside.read_text() == 'protected'
        if not all(checks.values()):
            raise RuntimeError('Permission checks failed: ' + json.dumps(checks))
        imports = 'import numpy,pandas,scipy,yaml,jsonschema,langgraph.graph,textual,psutil,packaging,keyring,pypdf;print("IMPORTS_OK")'
        run_check('python_dependencies', [*prefix, sys.executable, '-c', imports], diagnostics, env=env, cwd=task)
        packages = resource(root, 'native_dist/r-packages.txt').read_text().split()
        bioc = resource(root, 'native_dist/r-bioc-packages.txt')
        if bioc.is_file():
            packages += bioc.read_text().split()
        literal = lambda p: json.dumps(str(p), ensure_ascii=False)
        rcode = R_PROBE.replace('INPUT_REL', literal(item['path'])).replace('OUTSIDE', literal(outside))
        r_file = task/'probe.R'; r_file.write_text(rcode, encoding='utf-8')
        run_check('r_permissions_and_dependencies', [*prefix, rscript_path(root), '--vanilla', str(r_file), *packages], diagnostics, env=env, cwd=task)
        checks['r_native_read_write'] = (task/'r_probe.txt').read_text().strip() == 'R_OK'
        checks['probe_original_unchanged_after_r'] = approved.read_text() == 'approved' and outside.read_text() == 'protected'
        if not all(checks.values()):
            raise RuntimeError('Permission checks failed: ' + json.dumps(checks))
        diagnostics['phase'] = 'complete'
    return checks


PYTHON_PROBE = r'''import errno,json,os,pathlib,socket,sys
checks={}; errors={}
task=pathlib.Path(os.environ['TASK_DIR'])
approved=pathlib.Path(os.environ['PROJECT_ROOT'])/INPUT_REL
outside=pathlib.Path(OUTSIDE)
checks['approved_read']=approved.read_text()=='approved'
checks['secret_absent']='MULTIOMICS_PROBE_SECRET' not in os.environ
def denied(name,fn):
    try:
        fn(); checks[name]=False
    except OSError as e:
        checks[name]=e.errno in (errno.EACCES,errno.EPERM) or getattr(e,'winerror',None) in (5,10013)
        errors[name]={'type':type(e).__name__,'errno':e.errno,'winerror':getattr(e,'winerror',None)}
def write(p):
    with p.open('a') as f: f.write('changed')
denied('approved_write_denied',lambda:write(approved))
denied('outside_read_denied',lambda:outside.read_text())
denied('outside_write_denied',lambda:write(outside))
def connect():
    with socket.create_connection(('127.0.0.1',PORT),timeout=3): pass
denied('network_denied',connect)
if sys.platform=='linux':
    import ctypes
    libc=ctypes.CDLL(None,use_errno=True); fn=libc.process_vm_readv
    fn.argtypes=[ctypes.c_int,ctypes.c_void_p,ctypes.c_ulong,ctypes.c_void_p,ctypes.c_ulong,ctypes.c_ulong];fn.restype=ctypes.c_ssize_t
    checks['memory_read_syscall_denied']=fn(os.getpid(),None,0,None,0,0)==-1 and ctypes.get_errno()==1
elif sys.platform=='darwin':
    import ctypes
    libc=ctypes.CDLL(None,use_errno=True); fn=libc.sysctl
    fn.argtypes=[ctypes.POINTER(ctypes.c_int),ctypes.c_uint,ctypes.c_void_p,ctypes.POINTER(ctypes.c_size_t),ctypes.c_void_p,ctypes.c_size_t];fn.restype=ctypes.c_int
    mib=(ctypes.c_int*3)(1,49,CONTROLLER_PID);size=ctypes.c_size_t(0)
    checks['controller_environment_query_denied']=fn(mib,3,None,ctypes.byref(size),None,0)==-1 and ctypes.get_errno() in (1,13)
elif os.name=='nt':
    import ctypes
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];kernel.OpenProcess.restype=wintypes.HANDLE
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    h=kernel.OpenProcess(0x0010,False,CONTROLLER_PID)
    checks['controller_memory_open_denied']=not h and ctypes.get_last_error()==5
    if h: kernel.CloseHandle(h)
(task/'probe.json').write_text(json.dumps({'checks':checks,'errors':errors}),encoding='utf-8')
'''

R_PROBE = r'''Sys.setenv(LANGUAGE='en')
input <- file.path(Sys.getenv('PROJECT_ROOT'),INPUT_REL)
stopifnot(readLines(input,warn=FALSE)=='approved')
denied <- function(p) {
 messages <- character()
 failed <- tryCatch(withCallingHandlers({con<-file(p,'a');writeLines('changed',con);close(con);FALSE},
 warning=function(w){messages<<-c(messages,conditionMessage(w));invokeRestart('muffleWarning')}),
 error=function(e){messages<<-c(messages,conditionMessage(e));TRUE})
 isTRUE(failed) && any(grepl('Permission denied|Operation not permitted|Access is denied',messages,ignore.case=TRUE))
}
stopifnot(denied(input),denied(OUTSIDE))
for(p in commandArgs(TRUE)) if(!requireNamespace(p,quietly=TRUE)) stop(paste('Missing R package',p))
writeLines('R_OK',file.path(Sys.getenv('TASK_DIR'),'r_probe.txt'))
'''


def verify_manifest(root):
    from .layout import bundled_runtime
    if bundled_runtime(root):
        from .bundled import verify_bundle, verify_workspace_inputs
        installed = bundled_runtime(root)
        verify_bundle(Path(installed['bundle_root']))
        verify_workspace_inputs(Path(installed['bundle_root']), root)
        return {'status': 'passed', 'delivery': 'bundled'}
    path = root / "package_manifest.json"
    if not path.is_file():
        return {"status": "source_checkout"}
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("delivery") == "public_source":
        from .public_update import installed
        manifest = installed(root)
        return {"status": "passed", "files": len(manifest["files"]), "delivery": "public_source"}
    for item in manifest["files"]:
        target = root / item["path"]
        if target.is_symlink() or not target.is_file():
            raise ValueError("包文件缺失或为链接：" + item["path"])
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        if digest != item["sha256"]:
            raise ValueError("包文件校验失败：" + item["path"])
    return {"status": "passed", "files": len(manifest["files"])}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--project-root', type=Path, required=True)
    args = parser.parse_args()
    root = args.project_root.resolve()
    (root/'.runtime').mkdir(exist_ok=True)
    record = {'schema_version':2, 'status':'failed','platform':sys.platform,'machine':platform.machine(),
              'python':sys.version.split()[0], 'os_version':platform.platform(),
              'python_executable':sys.executable, 'phase':'package_integrity'}
    try:
        record['guard_fingerprint'] = guard_fingerprint(root)
        record['package'] = verify_manifest(root)
        record['phase'] = 'runtime_identity'
        record['runtime_fingerprint'] = runtime_fingerprint(root)
        record['phase'] = 'behavioral_selfcheck'
        record['probe_diagnostics'] = {}
        record['checks'] = probe(root, record['probe_diagnostics'])
        record['phase'] = 'credential_isolation'
        from multiomics_tui.llm import load_tui_config
        from multiomics_tui.credentials import enabled
        if enabled(load_tui_config(root)):
            from .credential_probe import probe as credential_probe
            record['credential_storage'] = credential_probe(root)
            if record['credential_storage']['status'] != 'passed':
                raise RuntimeError('System credential isolation has not passed; unlock the credential service and retry')
        record['status'] = 'passed'; record['phase'] = 'complete'
        print('本机检查通过：Python/R 启动、依赖、文件和网络限制及启用的凭据隔离均已验证。')
    except Exception as exc:
        from multiomics_tui.model_config import redact
        record['error_type'] = type(exc).__name__
        record['error'] = redact(str(exc))
        record['failed_stage'] = record.get('probe_diagnostics', {}).get('phase') if record['phase']=='behavioral_selfcheck' else record['phase']
        print('本机检查未通过：' + record['error'], file=sys.stderr)
    path = root/'.runtime/doctor.json'
    stage = path.with_suffix('.json.tmp')
    stage.write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n',encoding='utf-8'); stage.replace(path)
    return 0 if record['status']=='passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())

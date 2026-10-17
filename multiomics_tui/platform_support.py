"""Native paths, file leases and process identities; Linux job behavior stays intact."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def python_path(root: Path) -> Path:
    from native_dist.layout import bundled_runtime
    installed = bundled_runtime(root)
    if installed:
        return Path(installed['python'])
    return Path(root) / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")


def lock_fd(fd: int, blocking: bool = True) -> None:
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))


def unlock_fd(fd: int) -> None:
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)


def native_identity(pid):
    import psutil
    try:
        process = psutil.Process(int(pid))
        status = process.status()
        return {"state": "Z" if status == psutil.STATUS_ZOMBIE else "R",
                "pgid": os.getpgid(process.pid) if os.name != "nt" else process.pid,
                "start_ticks": str(process.create_time()), "boot_id": str(psutil.boot_time())}
    except (psutil.Error, OSError, TypeError, ValueError):
        return None


def distribution_enabled(root: Path) -> bool:
    # This flag is generated in the distribution only; original study config is untouched.
    import yaml
    try:
        path = Path(root) / "config/study_config.yaml"
        if not path.is_file():
            path = Path(root) / "config/study_config.example.yaml"
        cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cfg.get("distribution", {}).get("mode") == "native_collaborator_test"
    except (OSError, ValueError, yaml.YAMLError):
        return False


def require_supported_action(root: Path, action: str) -> None:
    if distribution_enabled(root) and action != "task":
        raise ValueError("公开版支持自然语言新分析任务。历史固定流程需要独立的研究配置和输入；请将本地数据放入 data/ 后输入分析目标。")


def start_native(manager, kind, argv, label, run_id, env_extra):
    """Supervisor acquires its own lease before launch, avoiding Windows fd inheritance."""
    import json
    import subprocess
    import time
    import uuid
    from .jobs import _atomic_json, _utc
    manager.job_dir().mkdir(parents=True, exist_ok=True)
    manager.log_dir().mkdir(parents=True, exist_ok=True)
    admission = (manager.root / "workspace/tui/admission.lock").open("a+b")
    try:
        try:
            lock_fd(admission.fileno(), blocking=False)
        except OSError as exc:
            raise ValueError("A TUI job is already starting") from exc
        lease = (manager.root / "workspace/tui/pipeline.lock").open("a+b")
        try:
            try:
                lock_fd(lease.fileno(), blocking=False)
            except OSError as exc:
                raise ValueError("A TUI job is already running or stopping") from exc
            if manager.active_pipeline():
                raise ValueError("A TUI job is already running or stopping")
            jid = time.strftime("%Y%m%d_%H%M%S", time.gmtime()) + "_" + uuid.uuid4().hex[:8]
            job = dict(version=2, job_id=jid, kind=kind, label=label, argv=list(map(str, argv)),
                       run_id=run_id, pid=None, pgid=None, child_identity=None,
                       supervisor_pid=None, supervisor_identity=None, status="starting",
                       started_at=_utc(), ended_at=None, exit_code=None, error=None,
                       stop_reason=None, key_injected=bool(env_extra),
                       log_path=manager.log_path(jid).relative_to(manager.root).as_posix())
            _atomic_json(manager._record_path(jid), job)
        finally:
            lease.close()
        env = dict(os.environ)
        if kind == 'task':
            from .model_config import task_environment
            env = task_environment()
        if env_extra:
            env.update({k: str(v) for k, v in env_extra.items() if v})
        flags = {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
        with manager.log_path(jid).open("ab") as stream:
            stream.write((f"# multiomics job {jid}\n").encode())
            try:
                proc = subprocess.Popen([sys.executable, str(Path(__file__).with_name("native_supervisor.py")),
                                         str(manager.root), jid], cwd=manager.root, env=env,
                                        stdin=subprocess.DEVNULL, stdout=stream,
                                        stderr=subprocess.STDOUT, close_fds=True, **flags)
            except OSError as exc:
                job.update(status="failed", ended_at=_utc(), error=str(exc))
                _atomic_json(manager._record_path(jid), job)
                return job
        manager._processes[jid] = proc
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = manager.get(jid)
            if current and current["status"] != "starting":
                return current
            if proc.poll() is not None:
                job.update(status="failed", exit_code=proc.returncode, ended_at=_utc(),
                           error="Native supervisor exited before handshake; inspect job log")
                _atomic_json(manager._record_path(jid), job)
                return job
            time.sleep(.02)
        return manager.get(jid) or job
    finally:
        admission.close()

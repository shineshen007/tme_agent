"""Native macOS/Windows detached owner; never infer success from stale outputs."""
from __future__ import annotations
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from multiomics_tui.jobs import _atomic_json, _utc, group_alive, process_info
from multiomics_tui.platform_support import lock_fd


def supervise(root: Path, job_id: str):
    path = root / "workspace/tui/jobs" / (job_id + ".json")
    stop_path = path.with_suffix(".stop")
    job = json.loads(path.read_text(encoding="utf-8"))
    process = win_job = None
    lease = (root / "workspace/tui/pipeline.lock").open("a+b")
    try:
        lock_fd(lease.fileno(), blocking=False)
        job.update(supervisor_pid=os.getpid(), supervisor_identity=process_info(os.getpid()))
        _atomic_json(path, job)
        if os.name == "nt":
            from multiomics_tui.windows_native import WindowsJob
            win_job = WindowsJob()
            process = win_job.launch(job["argv"], root)
        else:
            process = subprocess.Popen(job["argv"], cwd=root, stdin=subprocess.DEVNULL,
                                       start_new_session=True, close_fds=True)
        job.update(pid=process.pid, pgid=process.pid, child_identity=process_info(process.pid), status="running")
        _atomic_json(path, job)
        stopping = False
        deadline = None
        forced = False
        def alive():
            return win_job.alive() if win_job else group_alive(process.pid)
        def terminate(force=False):
            if win_job:
                win_job.terminate()
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
                except ProcessLookupError:
                    pass
        # macOS waitid with WNOWAIT keeps the leader's PID reserved until its
        # group drains. Use libc where Python does not expose waitid.
        while True:
            if win_job:
                code = process.poll()
            else:
                if sys.platform == "linux":  # Allows lifecycle tests on the build machine.
                    from multiomics_tui.supervisor import peek_exit
                    code = peek_exit(process)
                else:
                    from multiomics_tui.macos_guard import peek_exit
                    code = peek_exit(process.pid)
            living = alive()
            if code is not None and not living:
                code = process.wait() if not win_job else code
                job.update(status="killed" if stopping else ("succeeded" if code == 0 else "failed"),
                           exit_code=code, ended_at=_utc())
                _atomic_json(path, job)
                return
            if not stopping and stop_path.exists():
                request = json.loads(stop_path.read_text(encoding="utf-8"))
                if request.get("supervisor_identity") == job["supervisor_identity"]:
                    stopping = True
                    job.update(status="stopping", stop_reason="user_confirmed")
                    _atomic_json(path, job)
                    if living:
                        terminate()
                    deadline = time.monotonic() + 10
            if stopping and living and time.monotonic() >= deadline:
                if not forced:
                    terminate(True)
                    forced = True
                    deadline = time.monotonic() + 5
                    job["stop_reason"] = "user_confirmed_force_after_timeout"
                else:
                    job.update(status="stop_failed", error="Task tree still alive; lease retained")
                _atomic_json(path, job)
            time.sleep(.1)
    except BaseException as exc:
        job.update(error=f"{type(exc).__name__}: {exc}")
        if process:
            if win_job:
                win_job.terminate()
                while win_job.alive():
                    time.sleep(.1)
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                while group_alive(process.pid):
                    time.sleep(.1)
        job.update(status="failed", ended_at=_utc(), exit_code=process.wait() if process else None)
        _atomic_json(path, job)
    finally:
        if process and os.name == "nt":
            process.close()
        if win_job:
            win_job.close()
        lease.close()


if __name__ == "__main__":
    supervise(Path(sys.argv[1]).resolve(), sys.argv[2])

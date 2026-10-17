"""Detached job owner: durable exit status and bounded process-group cancellation."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

# Executable by absolute path even when supervising a temporary project fixture.
if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from multiomics_tui.jobs import _atomic_json, _utc, group_alive, process_info

TERM_SECONDS = 10.0
KILL_SECONDS = 5.0


def peek_exit(process):
    """Keep the group leader unreaped until its group drains, preventing PGID reuse."""
    info = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    if info is None:
        return None
    return info.si_status if info.si_code == os.CLD_EXITED else -info.si_status


def supervise(root, job_id, lease_fd):
    root = Path(root)
    path = root / 'workspace/tui/jobs' / f'{job_id}.json'
    stop_path = path.with_suffix('.stop')
    job = json.loads(path.read_text())
    job.update(supervisor_pid=os.getpid(), supervisor_identity=process_info(os.getpid()))
    _atomic_json(path, job)
    process = None
    stopping = False
    deadline = None
    forced = False
    try:
        process = subprocess.Popen(job['argv'], cwd=root, stdin=subprocess.DEVNULL,
                                   start_new_session=True, close_fds=True)
        job.update(pid=process.pid, pgid=process.pid, child_identity=process_info(process.pid), status='running')
        _atomic_json(path, job)
        while True:
            code = peek_exit(process)
            alive = group_alive(job['pgid'])
            if code is not None and not alive:
                process.wait()
                job.update(exit_code=code, status='killed' if stopping else ('succeeded' if code == 0 else 'failed'),
                           ended_at=_utc())
                _atomic_json(path, job)
                return
            if not stopping and stop_path.exists():
                request = json.loads(stop_path.read_text())
                if request.get('supervisor_identity') == job['supervisor_identity']:
                    stopping = True
                    job.update(status='stopping', stop_reason='user_confirmed')
                    _atomic_json(path, job)
                    if alive:
                        try:
                            os.killpg(job['pgid'], signal.SIGTERM)
                        except ProcessLookupError:
                            pass
                    deadline = time.monotonic() + TERM_SECONDS
            if stopping and alive and time.monotonic() >= deadline:
                if not forced:
                    try:
                        os.killpg(job['pgid'], signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    forced = True
                    job['stop_reason'] = 'user_confirmed_sigkill_after_timeout'
                    deadline = time.monotonic() + KILL_SECONDS
                    _atomic_json(path, job)
                elif job['status'] != 'stop_failed':
                    job.update(status='stop_failed', error='Process group stop timed out; keeping the job lock until exit')
                    _atomic_json(path, job)
            time.sleep(.1)
    except Exception as exc:
        job.update(error=f'{type(exc).__name__}: {exc}')
        if process is not None and group_alive(process.pid):
            # Keep the lease while descendants exist; never label live computation finished.
            job['status'] = 'stop_failed'
            _atomic_json(path, job)
            while group_alive(process.pid):
                time.sleep(.2)
        job.update(status='failed', exit_code=process.wait() if process else None, ended_at=_utc())
        _atomic_json(path, job)
    finally:
        os.close(lease_fd)


if __name__ == '__main__':
    supervise(sys.argv[1], sys.argv[2], int(sys.argv[3]))

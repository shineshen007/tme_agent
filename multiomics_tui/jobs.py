"""Persistent supervised jobs. UI reads state; the detached supervisor owns writes."""
from __future__ import annotations

try:
    import fcntl
except ImportError:  # Windows native leases live in platform_support.
    fcntl = None
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

JOB_DIR = 'workspace/tui/jobs'
LOG_DIR = 'workspace/tui/logs'
PIPELINE_KINDS = {'pipeline', 'module', 'task'}
ACTIVE = {'starting', 'running', 'stopping', 'stop_failed', 'stalled'}
TERMINAL = {'succeeded', 'failed', 'killed', 'exited'}


LEGACY_WARNINGS = {
    '监督进程已退出，需核查任务与子进程': 'Supervisor exited; review the job and child process',
    '历史任务：退出状态未经新版监督机制核验': 'Legacy job: exit status was not verified by the current supervisor',
}


def display_warning(value):
    text = str(value or '')
    return LEGACY_WARNINGS.get(text, text)


def _utc():
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def process_info(pid):
    """Linux PID identity and group; zombies no longer execute or write outputs."""
    if sys.platform != 'linux':
        from .platform_support import native_identity
        return native_identity(pid)
    try:
        raw = Path(f'/proc/{int(pid)}/stat').read_text()
        fields = raw[raw.rfind(')') + 2:].split()
        return {'state': fields[0], 'pgid': int(fields[2]), 'start_ticks': fields[19],
                'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip()}
    except (OSError, ValueError, IndexError, TypeError):
        return None


def same_process(pid, identity):
    info = process_info(pid)
    # Native psutil creation time identifies PID reuse. Its Windows boot-time
    # estimate can vary slightly across processes, so it is diagnostic only.
    keys = ('start_ticks', 'boot_id') if sys.platform == 'linux' else ('start_ticks',)
    return bool(info and identity and info['state'] not in {'Z', 'X'} and
                all(info[k] == identity.get(k) for k in keys))


def group_alive(pgid):
    if not pgid:
        return False
    if sys.platform != 'linux':
        import psutil
        for process in psutil.process_iter(['pid', 'status']):
            try:
                if os.getpgid(process.pid) == pgid and process.info['status'] != psutil.STATUS_ZOMBIE:
                    return True
            except (OSError, psutil.Error):
                continue
        return False
    for path in Path('/proc').glob('[0-9]*/stat'):
        info = process_info(path.parent.name)
        if info and info['pgid'] == pgid and info['state'] not in {'Z', 'X'}:
            return True
    return False


class JobManager:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self._processes = {}

    def job_dir(self):
        return self.root / JOB_DIR

    def log_dir(self):
        return self.root / LOG_DIR

    def _valid_id(self, job_id):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,100}', str(job_id)):
            raise ValueError('Invalid job ID')
        return str(job_id)

    def _record_path(self, job_id):
        return self.job_dir() / f'{self._valid_id(job_id)}.json'

    def log_path(self, job_id):
        return self.log_dir() / f'{self._valid_id(job_id)}.log'

    def start(self, kind, argv, label='', run_id=None, env_extra=None):
        """Transfer an exclusive file-lock lease to the supervisor, never the child."""
        if sys.platform != 'linux':
            from .platform_support import start_native
            return start_native(self, kind, argv, label, run_id, env_extra)
        self.job_dir().mkdir(parents=True, exist_ok=True)
        self.log_dir().mkdir(parents=True, exist_ok=True)
        lease = (self.root / 'workspace/tui/pipeline.lock').open('a+')
        try:
            try:
                fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError('A TUI job is already running or stopping') from exc
            active = self.active_pipeline()
            if active:
                raise ValueError(f"Job {active['job_id']} is {active['status']}; review it first")
            jid = time.strftime('%Y%m%d_%H%M%S', time.gmtime()) + '_' + uuid.uuid4().hex[:8]
            record = self._record_path(jid)
            log = self.log_path(jid)
            job = dict(version=2, job_id=jid, kind=kind, label=label, argv=list(map(str, argv)),
                       run_id=run_id, pid=None, pgid=None, child_identity=None,
                       supervisor_pid=None, supervisor_identity=None, status='starting',
                       started_at=_utc(), ended_at=None, exit_code=None, error=None,
                       stop_reason=None, key_injected=bool(env_extra),
                       log_path=str(log.relative_to(self.root)))
            _atomic_json(record, job)
            env = dict(os.environ)
            if kind == 'task':
                from .model_config import task_environment
                env = task_environment()
            if env_extra:
                env.update({k: str(v) for k, v in env_extra.items() if v})
            with log.open('ab') as stream:
                stream.write((f'# multiomics job {jid}\n# command={job["argv"]!r}\n\n').encode())
                stream.flush()
                try:
                    proc = subprocess.Popen(
                        [sys.executable, str(Path(__file__).with_name('supervisor.py')),
                         str(self.root), jid, str(lease.fileno())], cwd=self.root, env=env,
                        stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
                        start_new_session=True, pass_fds=(lease.fileno(),))
                except OSError as exc:
                    job.update(status='failed', error=str(exc), ended_at=_utc())
                    _atomic_json(record, job)
                    return job
            self._processes[jid] = proc
            # A short handshake makes immediate failures visible without blocking the UI.
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                result = self.get(jid)
                if result and result['status'] != 'starting':
                    return result
                time.sleep(.02)
            return self.get(jid) or job
        finally:
            # Do not LOCK_UN here: the inherited open-file description holds the lease.
            lease.close()

    def list_jobs(self, limit=50):
        jobs = []
        for path in sorted(self.job_dir().glob('*.json'), reverse=True):
            try:
                jobs.append(self.refresh(json.loads(path.read_text(encoding='utf-8'))))
            except (OSError, ValueError, TypeError):
                continue
        return jobs if limit is None else jobs[:limit]

    def get(self, job_id):
        try:
            return self.refresh(json.loads(self._record_path(job_id).read_text(encoding='utf-8')))
        except (OSError, json.JSONDecodeError):
            return None

    def refresh(self, job):
        """Never promote an operation using the underlying analysis' historical state."""
        result = dict(job)
        proc = self._processes.get(job.get('job_id'))
        if proc:
            proc.poll()  # reap only, persistent state remains supervisor-owned
        if job.get('version') == 2:
            if job.get('status') in ACTIVE and job.get('supervisor_pid') and not same_process(
                    job['supervisor_pid'], job.get('supervisor_identity')):
                result.update(status='stalled', warning='Supervisor exited; review the job and child process')
        else:
            result['warning'] = 'Legacy job: exit status was not verified by the current supervisor'
            if job.get('exit_code') is not None:
                result['status'] = 'succeeded' if job['exit_code'] == 0 else 'failed'
            else:
                info = process_info(job.get('pid'))
                result['status'] = 'running' if info and info['state'] not in {'Z', 'X'} else 'exited'
        if job.get('run_id'):
            try:
                result['run_state'] = json.loads((self.root / 'workspace/runs' / job['run_id'] /
                                                  'run_state.json').read_text())
            except (OSError, ValueError):
                result['run_state'] = None
        return result

    @staticmethod
    def _pid_alive(pid):
        info = process_info(pid)
        return bool(info and info['state'] not in {'Z', 'X'})

    def tail(self, job_id, lines=200):
        path = self.log_path(job_id)
        try:
            return '\n'.join(path.read_text(encoding='utf-8', errors='replace').splitlines()[-max(1, lines):])
        except OSError as exc:
            return f'(Could not read log: {exc})'

    def kill(self, job_id):
        job = self.get(job_id)
        if not job:
            raise ValueError(f'Unknown job: {job_id}')
        if job.get('version') != 2:
            raise ValueError('Legacy job has no verifiable process identity; review it manually')
        if job['status'] in TERMINAL:
            return job
        if not same_process(job.get('supervisor_pid'), job.get('supervisor_identity')):
            raise ValueError('Supervisor identity mismatch; no stop request was sent; review manually')
        _atomic_json(self.job_dir() / f'{self._valid_id(job_id)}.stop',
                     {'requested_at': _utc(), 'reason': 'user_confirmed',
                      'supervisor_identity': job['supervisor_identity']})
        return {**job, 'status': 'stopping'}

    def active_pipeline(self):
        return next((j for j in self.list_jobs(limit=None)
                     if j.get('kind') in PIPELINE_KINDS and j.get('status') in ACTIVE), None)

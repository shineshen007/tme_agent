"""Installed-environment lifecycle checks, using synthetic files and no API key."""
from __future__ import annotations
import argparse
import asyncio
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time


def wait_job(manager, job_id, timeout=15):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        job = manager.get(job_id)
        if job['status'] in {'succeeded','failed','killed','exited'}:
            return job
        time.sleep(0.05)
    raise RuntimeError('任务状态超时：'+job_id)


async def interface(root):
    from multiomics_tui.app import MultiomicsApp
    from multiomics_tui.controller import Session
    app = MultiomicsApp(Session(root,allow_llm=False))
    async with app.run_test(size=(80,24)) as pilot:
        await pilot.pause()
        await pilot.press('f1')
        await pilot.pause()
        await pilot.press('escape')
        await pilot.pause()
    return True


def acceptance(root):
    from multiomics_tui.jobs import JobManager, process_info, same_process
    from native_dist.doctor import require_probe
    require_probe(root)
    directory = root/'.runtime/lifecycle_acceptance'
    directory.mkdir(parents=True,exist_ok=True)
    manager = JobManager(root)
    checks = {'interface_start_help_close':asyncio.run(interface(root))}
    # The process which submitted this job exits immediately. Its detached
    # supervisor must still finish and record the actual status.
    output = directory/('continued_'+str(time.time_ns())+'.txt')
    code = 'import pathlib,time;time.sleep(1);pathlib.Path('+repr(str(output))+').write_text("continued")'
    submission = 'import json,sys;from pathlib import Path;from multiomics_tui.jobs import JobManager;j=JobManager(Path(sys.argv[1])).start("task",[sys.executable,"-c",sys.argv[2]],label="Bundled exit acceptance");print(json.dumps({"job_id":j["job_id"]}))'
    result = subprocess.run([sys.executable,'-c',submission,str(root),code],env=os.environ,
                            capture_output=True,text=True,timeout=15,check=True)
    job_id = json.loads(result.stdout.strip())['job_id']
    job = wait_job(manager,job_id)
    checks['submitter_exit_background_continues'] = output.read_text()=='continued'
    checks['real_exit_code'] = job['status']=='succeeded' and job['exit_code']==0
    # Stopping the parent must also stop the child, not leave an orphan writer.
    child_file = directory/('child_'+str(time.time_ns())+'.txt')
    code = 'import subprocess,sys,pathlib,time;p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"]);pathlib.Path('+repr(str(child_file))+').write_text(str(p.pid));time.sleep(60)'
    job = manager.start('task',[sys.executable,'-c',code],label='Bundled stop acceptance')
    child_identity = None
    try:
        deadline = time.monotonic()+10
        while not child_file.exists() and time.monotonic()<deadline:
            time.sleep(0.05)
        child_pid = int(child_file.read_text())
        child_identity = process_info(child_pid)
        if child_identity is None:
            raise RuntimeError('未能核验测试子进程身份')
        try:
            manager.start('task',[sys.executable,'-c','print("duplicate")'],label='Expected rejection')
            checks['concurrent_task_rejected'] = False
        except ValueError:
            checks['concurrent_task_rejected'] = True
        manager.kill(job['job_id'])
        final = wait_job(manager,job['job_id'])
        checks['stop_actual_child'] = not same_process(child_pid,child_identity)
        checks['stop_status_recorded'] = final['status']=='killed' and final['exit_code'] is not None
        checks['lease_released'] = manager.active_pipeline() is None
    finally:
        if manager.get(job['job_id'])['status'] in {'starting','running','stopping','stalled','stop_failed'}:
            manager.kill(job['job_id'])
    if not all(checks.values()):
        raise RuntimeError('生命周期验收未通过：'+json.dumps(checks,ensure_ascii=False))
    return checks


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--project-root',type=Path,required=True)
    args = parser.parse_args()
    root = args.project_root.resolve()
    record = dict(status='failed',platform=sys.platform,machine=platform.machine(),python=sys.version.split()[0])
    try:
        record['checks'] = acceptance(root)
        record['status'] = 'passed'
    except Exception as exc:
        from multiomics_tui.model_config import redact
        record['error'] = redact(f'{type(exc).__name__}: {exc}')
    (root/'.runtime/bundled_acceptance.json').write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(record,ensure_ascii=False,indent=2))
    return 0 if record['status']=='passed' else 1


if __name__=='__main__':
    raise SystemExit(main())

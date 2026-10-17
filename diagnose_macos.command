#!/bin/bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "请把本文件放到与 setup_macos.command 相同的项目文件夹，先完成依赖安装。"
  read -r -p "按回车关闭" _
  exit 1
fi
if .venv/bin/python - <<'PY'
from pathlib import Path
import json, os, platform, signal, subprocess, sys, tempfile, time, traceback

root = Path.cwd().resolve()
if sys.platform != 'darwin':
    print('此诊断入口仅用于 macOS。')
    raise SystemExit(1)
from multiomics_tui.model_config import redact
from multiomics_tui.task_runner import _step_environment
from multiomics_tui.actions import rscript_path
from multiomics_tui.temp_paths import cleanup
from orchestrator.io import load_config

runtime = root / '.runtime'
runtime.mkdir(exist_ok=True)
record = {'purpose': 'installation diagnostic; synthetic probes only',
          'platform': platform.platform(), 'machine': platform.machine(),
          'python': sys.version, 'executable': sys.executable,
          'resolved_executable': str(Path(sys.executable).resolve()),
          'prefix': sys.prefix, 'base_prefix': sys.base_prefix,
          'sandbox_exec_exists': Path('/usr/bin/sandbox-exec').is_file(),
          'steps': {}, 'generated_at': time.strftime('%Y-%m-%d %H:%M:%S')}
pids = []
started_at = time.time()

def collect(label, argv, env=None, cwd=None, timeout=35):
    step = {'argv': list(map(str, argv))}
    try:
        with subprocess.Popen(list(map(str, argv)), env=env, cwd=cwd or root,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, start_new_session=True) as process:
            pids.append(process.pid)
            try:
                out, err = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                out, err = process.communicate()
                step['timed_out'] = True
            step.update(pid=process.pid, returncode=process.returncode,
                        stdout=redact(out[-6000:]), stderr=redact(err[-6000:]))
            if process.returncode < 0:
                step['signal'] = signal.Signals(-process.returncode).name
    except Exception as exc:
        step['error'] = redact(f'{type(exc).__name__}: {exc}')
    record['steps'][label] = step
    print(label + ': ' + str(step.get('returncode', step.get('error'))), flush=True)
    if step.get('signal'):
        print('  系统信号：' + step['signal'], flush=True)
    return step

try:
    collect('macos_version', ['/usr/bin/sw_vers'])
    collect('python_signature', ['/usr/bin/codesign', '-dv', '--verbose=2', str(Path(sys.executable).resolve())])
    collect('python_without_guard', [sys.executable, '-c', 'import json,pathlib,socket;print("PYTHON_OK")'])
    cfg = load_config(root)
    rscript = rscript_path(root)
    collect('r_without_guard', [rscript, '--vanilla', '-e', 'cat(R.version.string,"\\n")'])
    (root / 'analysis').mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='task_macos_diagnostic_', dir=root / 'analysis') as name:
        task = Path(name)
        (task / 'tmp').mkdir()
        try:
            env = _step_environment(root, task, cfg, [])
            prefix = [sys.executable, str(root / 'multiomics_tui/task_guard.py'), str(task), '--']
            from multiomics_tui.macos_guard import execution_policy
            record['sandbox_rules'] = {}
            for label, executable in [('python', sys.executable), ('r', rscript)]:
                policy, mappings = execution_policy(task, [executable], env)
                record['sandbox_rules'][label] = {'policy': policy, 'runtime_paths': mappings}
            collect('python_guard_startup', [*prefix, sys.executable, '-c', 'import json,pathlib,socket;print("GUARDED_PYTHON_OK")'], env=env, cwd=task)
            collect('r_guard_startup', [*prefix, rscript, '--vanilla', '-e', 'cat("GUARDED_R_OK\\n")'], env=env, cwd=task)
            print("下面是启动对照诊断：without_root_read 分支可能预期失败，不代表新自检失败。", flush=True)
            from native_dist.macos_startup import root_read_ab
            record['root_directory_ab'] = root_read_ab(record['sandbox_rules']['python']['policy'], {
                'python': [sys.executable, '-c', 'import json,pathlib,socket;print("PYTHON_AB_OK")'],
                'r': [rscript, '--vanilla', '-e', 'cat("R_AB_OK\\n")']}, env, task, collect)
        finally:
            cleanup(root, task.name)
    # Existing behavior probe uses only its own synthetic files. It does not
    # replace doctor.json or modify any distributed source/configuration.
    if record['steps'].get('python_guard_startup', {}).get('returncode') == 0:
        from native_dist.doctor import probe
        try:
            record['full_probe_diagnostics'] = {}
            record['full_probe'] = {'checks': probe(root, record['full_probe_diagnostics']), 'status': 'passed'}
        except Exception as exc:
            record['full_probe'] = {'status': 'failed', 'error': redact(f'{type(exc).__name__}: {exc}')}
    # Collect only events tied to the diagnostic children, with no sudo.
    # Sandbox kernel messages use the child's PID in parentheses.
    filter_pids = [p for p in pids if p > 0]
    if filter_pids:
        predicate = ' OR '.join(f'(processID == {pid} OR eventMessage CONTAINS "({pid})")' for pid in filter_pids)
        collect('macos_process_logs', ['/usr/bin/log', 'show', '--last', '5m', '--style', 'compact', '--predicate', predicate], timeout=25)
        from native_dist.macos_startup import crash_summaries
        record['crash_summaries'] = crash_summaries(set(filter_pids), started_at)
except Exception as exc:
    record['diagnostic_error'] = redact(f'{type(exc).__name__}: {exc}')
    record['traceback'] = redact(traceback.format_exc())
finally:
    output = runtime / 'macos_diagnostic.json'
    output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('\n诊断记录已保存：' + str(output))
    print('请将 macos_diagnostic.json 发给项目负责人；不要发送密钥或整个 .runtime。')
PY
then
  MULTIOMICS_DIAG_EXIT=0
else
  MULTIOMICS_DIAG_EXIT=$?
fi
if .venv/bin/python -B -m native_dist.diagnostics --project-root "$PWD"; then
  MULTIOMICS_DIAG_EXIT=0
else
  MULTIOMICS_DIAG_EXIT=$?
fi
read -r -p "按回车关闭" _
exit "$MULTIOMICS_DIAG_EXIT"

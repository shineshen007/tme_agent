"""Real task-environment installation checks; synthetic inputs, no model credentials."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import uuid

import yaml


def acceptance(project_root):
    from multiomics_tui.task_environment import Installer, bound_config, inventory
    from multiomics_tui.planning_evidence import probe_method_apis
    from multiomics_tui.task_runner import _step_environment, _check_code
    project_root = Path(project_root).resolve()
    root = project_root / '.runtime' / ('environment_acceptance_' + uuid.uuid4().hex)
    root.mkdir(parents=True)
    rscript = shutil.which('Rscript')
    if not rscript:
        raise RuntimeError('Acceptance requires an existing R interpreter')
    if sys.platform == 'linux' and not shutil.which('bwrap'):
        raise RuntimeError('Acceptance requires existing bubblewrap; no system installation attempted')
    if os.name == 'nt':
        from native_dist.windows_runtime import prepare
        from native_dist.environment import inspect_python, requirements
        from native_dist.bootstrap import run, captured_run, dependency_report
        info = inspect_python(sys.executable, requirements(project_root))
        library = captured_run([rscript, '--vanilla', '-e', 'cat(find.package("jsonlite"))']).stdout.strip()
        private_r, _ = prepare(project_root, sys.executable, info, rscript, Path(library).parent,
                               ['jsonlite', 'digest'], dependency_report(sys.executable, project_root), run, captured_run)
        rscript = str(private_r)
    r_home = subprocess.check_output([rscript, '--vanilla', '-e', 'cat(R.home())'], text=True).strip()
    cfg = {'runtime': {'rscript': rscript, 'r_home': r_home}}
    (root / 'config').mkdir()
    (root / 'config/runtime.local.yaml').write_text(yaml.safe_dump(cfg), encoding='utf-8')
    folder = root / '.runtime/task_environments/acceptance/attempt_native'
    folder.mkdir(parents=True)
    backend = Installer(root, folder, cfg, print)
    python, python_locks = backend.python([{'language':'python','package':'six','version':'1.17.0',
        'source':{'kind':'pypi'},'apis':['six.ensure_str']}])
    library, r_locks = backend.r([{'language':'r','package':'crayon','version':'1.5.3',
        'source':{'kind':'cran'},'apis':['red']}])
    receipt = {'directory':folder.relative_to(root).as_posix(),'python':python,'r_library':library}
    cfg = bound_config(cfg, receipt)
    probe = folder / 'probe'
    probe.mkdir()
    candidates = [{'id':'six','language':'python','package':'six','apis':['six.ensure_str']},
                  {'id':'crayon','language':'r','package':'crayon','apis':['red']}]
    rows = probe_method_apis(root, candidates, cfg, probe)
    if len(rows) != 2 or any(row['status'] != 'verified' for row in rows):
        raise RuntimeError('Installed API acceptance failed: ' + json.dumps(rows))
    before = inventory(folder / 'runtime')
    outputs = {}
    for language, suffix, code, executable in [
        ('python','.py','import six\nprint(six.ensure_str(b"BOUND_PYTHON_OK"))\n',python),
        ('r','.R','cat(crayon::strip_style(crayon::red("BOUND_R_OK")))\n',rscript)]:
        task = root / 'analysis' / language
        task.mkdir(parents=True)
        script = task / ('check' + suffix)
        script.write_text(code, encoding='utf-8')
        env = _step_environment(root, task, cfg, [])
        _check_code(root, script, language, env=env)
        args = [sys.executable, str(project_root/'multiomics_tui/task_guard.py'), str(task), '--', executable]
        args += ['--vanilla'] if language == 'r' else []
        result = subprocess.run([*args,str(script)],env=env,cwd=task,capture_output=True,text=True,timeout=60,check=True)
        expected = 'BOUND_R_OK' if language == 'r' else 'BOUND_PYTHON_OK'
        if result.stdout.strip().splitlines()[-1:] != [expected]:
            raise RuntimeError('Unexpected bound-runtime output: ' + result.stdout)
        outputs[language] = {'result':expected, 'stdout':result.stdout, 'stderr':result.stderr}
    if before != inventory(folder/'runtime'):
        raise RuntimeError('Execution changed installed runtime files')
    report = {'status':'passed','platform':platform.platform(),'root':str(root),
              'locks':{**python_locks,**r_locks},'apis':rows,'execution':outputs,
              'scope':'real package installation and native bound execution; synthetic inputs'}
    (root/'acceptance.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps({'status':'passed','report':str(root/'acceptance.json')}))
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--project-root', type=Path, default=Path.cwd())
    args = parser.parse_args()
    acceptance(args.project_root)


if __name__ == '__main__':
    main()

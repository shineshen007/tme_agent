"""Linux installation sandbox: read-only runtimes, private writable staging.

Package managers need chmod/utime during extraction. The analysis guard forbids
those syscalls globally, so installation uses bubblewrap's read-only mounts
instead. No research inputs, home directory, network or credentials are mounted.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys


def command(stage, argv, env):
    executable = shutil.which('bwrap')
    if not executable:
        raise RuntimeError('Offline package installation requires the existing bubblewrap system tool on Linux')
    stage = Path(stage).resolve(strict=True)
    paths = [Path(p) for p in ('/usr', '/bin', '/lib', '/lib64', '/etc') if Path(p).exists()]
    paths += [Path(sys.prefix), Path(sys.base_prefix)]
    paths += [Path(p) for p in json.loads(env.get('TASK_ALLOWED_READS', '[]'))]
    mounts = []
    for path in paths:
        if not path.is_absolute():
            raise ValueError('Installer runtime paths must be absolute')
        path = path.resolve(strict=True)
        if path == stage or path.is_relative_to(stage):
            continue
        if stage.is_relative_to(path):
            raise ValueError('Installer cannot mount a parent of its staging directory')
        if path not in mounts:
            mounts.append(path)
    result = [executable, '--unshare-all', '--die-with-parent', '--new-session', '--proc', '/proc', '--dev', '/dev']
    for path in sorted(mounts, key=lambda p: len(p.parts)):
        result += ['--ro-bind', str(path), str(path)]
    result += ['--bind', str(stage), str(stage), '--chdir', str(stage), '--', *argv]
    return result


def execute(stage, argv, env):
    args = command(stage, argv, env)
    os.execvpe(args[0], args, env)

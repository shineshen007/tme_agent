"""Build and verify a private Windows runtime before replacing an old copy."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid


def tree_manifest(root):
    records = {}
    for p in sorted(root.rglob('*')):
        if p.is_symlink() or getattr(p, 'is_junction', lambda: False)():
            raise ValueError('Runtime copy contains a link: ' + str(p))
        if p.is_file() and p.name != 'runtime_manifest.json' and '__pycache__' not in p.parts and p.suffix != '.pyc':
            records[p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return records


def verify_tree(root, identity):
    try:
        saved = json.loads((root/'runtime_manifest.json').read_text(encoding='utf-8'))
        return saved['identity'] == identity and saved['files'] == tree_manifest(root)
    except (OSError, ValueError, KeyError):
        return False


def commit_tree(stage, target):
    """Keep the old tree recoverable; roll back a failed switch."""
    backup = target.with_name('sandbox_runtime_previous_' + uuid.uuid4().hex)
    if target.exists():
        target.rename(backup)
    try:
        stage.rename(target)
    except BaseException:
        if backup.exists():
            backup.rename(target)
        raise
    return str(backup) if backup.exists() else None


def prepare(root, py, python_info, rscript, library, packages, dependencies, run, capture):
    from .environment import inspect_python, requirements
    root = Path(root)
    target = root/'.runtime/sandbox_runtime'
    literal = lambda p: json.dumps(str(p), ensure_ascii=False)
    r_home = Path(capture([str(rscript), '--vanilla', '-e', 'cat(R.home())']).stdout.strip())
    code = '.libPaths(c(' + literal(library) + ',.libPaths())); pk<-commandArgs(TRUE); ip<-installed.packages(); ds<-unique(c(pk,unlist(tools::package_dependencies(pk,db=ip,recursive=TRUE,which=c("Depends","Imports","LinkingTo"))))); for(p in ds) if(p!="R") cat(find.package(p),"\\n",sep="")'
    sources = [Path(p) for p in capture([str(rscript), '--vanilla', '-e', code, *packages]).stdout.strip().splitlines()]
    identity = {'python_version': python_info.get('version'), 'python_base': python_info['base_prefix'],
                'dependencies': dependencies['dependency_closure'], 'r_home': str(r_home),
                'r_executable': hashlib.sha256(Path(rscript).read_bytes()).hexdigest(),
                'r_packages': {p.name: hashlib.sha256((p/'DESCRIPTION').read_bytes()).hexdigest() for p in sources}}

    def validate(path):
        private_py = path/'python/python.exe'
        private_r = next(iter(sorted((path/'r').glob('bin/**/Rscript.exe'))), None)
        if private_r is None:
            raise RuntimeError('Private Rscript.exe is missing')
        info = inspect_python(private_py, requirements(root), include_user_site=False)
        if not info.get('compatible') or any(p['status'] != 'reusable' for p in info.get('packages', [])):
            raise RuntimeError('Private Python dependency imports failed')
        run([private_py, '-B', '-m', 'native_dist.package_copy', '--check', '--project-root', root],
            cwd=root, env={**os.environ, 'PYTHONNOUSERSITE': '1'})
        env = {k:v for k,v in os.environ.items() if k not in {'R_HOME','RHOME','R_LIBS','R_LIBS_SITE','R_LIBS_USER','PYTHONHOME','PYTHONPATH'}}
        env.update(R_HOME=str(path/'r'), R_LIBS_USER=str(path/'r/library'), PYTHONDONTWRITEBYTECODE='1')
        rcode = 'for(p in commandArgs(TRUE)) { if(!requireNamespace(p,quietly=TRUE)) stop(p); if(!startsWith(normalizePath(find.package(p),winslash="/"),normalizePath(R.home(),winslash="/"))) stop("External R package") };cat("PRIVATE_R_OK")'
        run([private_r, '--vanilla', '-e', rcode, *packages], env=env)
        return private_r

    if verify_tree(target, identity):
        try:
            private_r = validate(target)
            return private_r, {'action':'reuse_verified_windows_runtime', 'path':str(target)}
        except (OSError, RuntimeError, subprocess.CalledProcessError):
            pass
    stage = root/'.runtime'/('sandbox_runtime_build_' + uuid.uuid4().hex)
    stage.mkdir()
    # Failed staging copies are retained for diagnosis; no recursive removal of user paths.
    shutil.copytree(Path(python_info['base_prefix']), stage/'python',
                    ignore=shutil.ignore_patterns('site-packages','__pycache__','*.pyc'))
    run([py, '-B', '-m', 'native_dist.package_copy', '--project-root', root,
         '--destination', stage/'python/Lib/site-packages'], cwd=root,
        env={**os.environ, 'PYTHONNOUSERSITE': '1'})
    shutil.copytree(r_home, stage/'r', ignore=shutil.ignore_patterns('__pycache__'))
    for source in sources:
        shutil.copytree(source, stage/'r/library'/source.name, dirs_exist_ok=True)
    private_r = validate(stage)
    relative_r = private_r.relative_to(stage)
    (stage/'runtime_manifest.json').write_text(json.dumps({'identity':identity,'files':tree_manifest(stage)},indent=2),encoding='utf-8')
    backup = commit_tree(stage, target)
    return target/relative_r, {'action':'rebuild_windows_runtime','path':str(target),'previous':backup,'download':False}

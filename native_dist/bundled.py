"""Offline startup of installed applications with a persistent user workspace."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def within(base, relative):
    part = Path(relative)
    if part.is_absolute() or '..' in part.parts:
        raise ValueError('Unsafe bundle path: ' + str(relative))
    path = (Path(base) / part).resolve(strict=True)
    if not path.is_relative_to(Path(base).resolve()):
        raise ValueError('Bundle path resolves outside application: ' + str(relative))
    return path


def verify_bundle(bundle):
    bundle = Path(bundle).resolve(strict=True)
    manifest = json.loads((bundle/'bundle_manifest.json').read_text(encoding='utf-8'))
    for item in manifest['files']:
        path = within(bundle, item['path'])
        if item.get('link') is not None:
            original = bundle / item['path']
            if not original.is_symlink() or os.readlink(original) != item['link']:
                raise ValueError('应用链接校验失败：' + item['path'])
        elif not path.is_file() or path.stat().st_size != item['bytes'] or digest(path) != item['sha256']:
            raise ValueError('应用文件校验失败：' + item['path'])
    return manifest


def verify_workspace_inputs(bundle, workspace):
    metadata = json.loads((Path(bundle)/'bundle.json').read_text(encoding='utf-8'))
    for item in metadata['inputs']:
        path = within(workspace, item['path'])
        if digest(path) != item['sha256']:
            raise ValueError('原始输入副本发生变化，未覆盖：' + item['path'])


def default_workspace():
    return Path.home() / 'Documents/TME Multiomics Test'


def initialize(bundle, workspace):
    import yaml
    from multiomics_tui.jobs import _atomic_json, JobManager
    from multiomics_tui.platform_support import lock_fd, unlock_fd
    bundle = Path(bundle).resolve(strict=True)
    workspace = Path(workspace).resolve()
    if workspace == bundle or workspace.is_relative_to(bundle):
        raise ValueError('请选择应用目录外的工作目录。')
    metadata = json.loads((bundle/'bundle.json').read_text(encoding='utf-8'))
    if metadata['system'] != sys.platform or metadata['machine'] not in {platform.machine(), {'AMD64':'x86_64','arm64':'aarch64'}.get(platform.machine())}:
        raise RuntimeError('安装包与当前操作系统/芯片不匹配。')
    verify_bundle(bundle)
    app = within(bundle, 'app')
    marker = workspace/'.runtime/application.json'
    if (workspace/'config/study_config.yaml').exists() and not marker.exists():
        raise ValueError('该目录是已有源码项目，不能直接改为安装版工作目录。请另选空目录。')
    workspace.mkdir(parents=True, exist_ok=True)
    for relative in ('.runtime', 'config', '.runtime/application.json', 'config/api.local.yaml', 'config/runtime.local.yaml'):
        path = workspace / relative
        if path.is_symlink() or not path.resolve().is_relative_to(workspace):
            raise ValueError('工作目录配置不能为外部链接：' + relative)
    (workspace/'.runtime').mkdir(exist_ok=True)
    lease = (workspace/'.runtime/initialize.lock').open('a+b')
    acquired = False
    try:
        lock_fd(lease.fileno(), blocking=False)
        acquired = True
        old = json.loads(marker.read_text(encoding='utf-8')) if marker.is_file() else None
        if old and (old['bundle_root'] != str(bundle) or old['version'] != metadata['version']):
            if JobManager(workspace).active_pipeline() is not None:
                raise RuntimeError('后台任务仍运行，请完成或停止后再切换应用版本。')
        # Seed only immutable inputs/resources and configuration. No historical
        # result directories are copied; existing files are never overwritten.
        seed = [*metadata['inputs'], *metadata.get('resources', [])]
        for item in seed:
            source = within(app, item['path'])
            destination = workspace/item['path']
            if not destination.resolve().is_relative_to(workspace) or any(p.is_symlink() for p in [destination, *destination.parents] if p.is_relative_to(workspace)):
                raise ValueError('工作目录输入不能为符号链接：' + item['path'])
            if destination.exists():
                if digest(destination) != item['sha256']:
                    raise ValueError('已有输入/资源与此版本不同，未覆盖：' + item['path'])
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                # Exclusive destination avoids overwriting an input during a race.
                with source.open('rb') as src, destination.open('xb') as dst:
                    shutil.copyfileobj(src, dst)
                if digest(destination) != item['sha256']:
                    raise ValueError('输入复制校验失败：' + item['path'])
        (workspace/'config').mkdir(exist_ok=True)
        study = workspace/'config/study_config.yaml'
        if not study.exists():
            with within(app, 'config/study_config.yaml').open('rb') as src, study.open('xb') as dst:
                shutil.copyfileobj(src, dst)
        api = workspace/'config/api.local.yaml'
        if not api.exists():
            api.write_text('# 用 /model 配置服务；密钥不得写入此文件。\nllm: {}\n', encoding='utf-8')
        python = within(bundle, metadata['python'])
        rhome = within(bundle, metadata['r_home'])
        rscript = within(bundle, metadata['rscript'])
        libraries = [within(bundle, name) for name in metadata['r_libraries']]
        if os.name == 'nt':
            # AppContainer ACLs apply only to these private copies, never to the
            # installed application or user originals. Copy once per runtime ID.
            private = workspace/'.runtime/sandbox_runtime'
            receipt = private/'runtime_id.txt'
            runtime_id = metadata['runtime_id']
            if private.exists() and (not receipt.is_file() or receipt.read_text().strip() != runtime_id):
                if JobManager(workspace).active_pipeline() is not None:
                    raise RuntimeError('后台任务仍使用旧运行库，请完成或停止后再升级。')
                import uuid
                backup = private.with_name('sandbox_runtime_previous_'+uuid.uuid4().hex[:12])
                private.rename(backup)  # Preserve the previous private copy for rollback.
            if not private.exists():
                private.mkdir()
                shutil.copytree(within(bundle, metadata['python_home']), private/'python')
                shutil.copytree(rhome, private/'r')
                receipt.write_text(runtime_id+'\n')
            rhome = private/'r'
            rscript = next(iter(sorted(rhome.glob('bin/**/Rscript.exe'))), None)
            if rscript is None:
                raise RuntimeError('私有 R 库中未找到 Rscript.exe。')
            libraries = [rhome/'library']
        runtime = {'python': str(python), 'rscript': str(rscript), 'r_home': str(rhome),
                   'r_library_paths': list(map(str, libraries))}
        local = workspace/'config/runtime.local.yaml'
        temp = local.with_suffix('.yaml.tmp')
        temp.write_text(yaml.safe_dump({'runtime':runtime},allow_unicode=True),encoding='utf-8')
        temp.replace(local)
        value = dict(bundle_root=str(bundle), app_root=str(app), python=str(python),
                     version=metadata['version'], runtime_id=metadata['runtime_id'])
        _atomic_json(marker, value)
        verify_workspace_inputs(bundle, workspace)
        return runtime
    finally:
        if acquired:
            unlock_fd(lease.fileno())
        lease.close()


def launch(bundle, workspace, verify_only=False, smoke=False):
    app = within(bundle, 'app')
    os.environ.update(MULTIOMICS_APP_ROOT=str(app), PYTHONPATH=str(app),
                      PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1')
    runtime = initialize(bundle, workspace)
    env = dict(os.environ)
    env.pop('PYTHONHOME', None)
    env.pop('R_LIBS', None)
    env.update(R_HOME=runtime['r_home'], RHOME=runtime['r_home'], R_LIBS_USER=os.pathsep.join(runtime['r_library_paths']),
               R_LIBS_SITE=os.pathsep.join(runtime['r_library_paths']), PYTHONNOUSERSITE='1',
               MPLCONFIGDIR=str(workspace/'.cache/matplotlib'), XDG_CACHE_HOME=str(workspace/'.cache'),
               LANGSMITH_TRACING='false', LANGCHAIN_TRACING_V2='false')
    # No pip, R install.packages or system-runtime discovery on the end user's PC.
    subprocess.run([runtime['python'], '-m', 'native_dist.doctor', '--project-root', str(workspace)], cwd=workspace, env=env, check=True)
    if verify_only:
        return 0
    if smoke:
        return subprocess.call([runtime['python'], '-m', 'native_dist.smoke_task', '--project-root', str(workspace), '--http-model'], cwd=workspace, env=env)
    return subprocess.call([runtime['python'], '-m', 'multiomics_tui', '--project-root', str(workspace)], cwd=workspace, env=env)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--bundle-root',type=Path,required=True)
    parser.add_argument('--workspace',type=Path,default=default_workspace())
    parser.add_argument('--verify-only',action='store_true')
    parser.add_argument('--smoke',action='store_true')
    args = parser.parse_args()
    try:
        return launch(args.bundle_root.resolve(),args.workspace.resolve(),args.verify_only,args.smoke)
    except Exception as exc:
        from multiomics_tui.model_config import redact
        print('启动未完成：'+redact(f'{type(exc).__name__}: {exc}'),file=sys.stderr)
        print('请保留工作目录 .runtime/doctor.json；不要发送密钥。',file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

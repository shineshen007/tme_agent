"""Portable v8 regressions: dependency awareness and macOS sandbox defaults.

These run on any platform; native enforcement (real Seatbelt/R) needs macOS.
"""
import json
import platform
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


def test_entry_scripts_are_executable_in_git_index():
    """Git 工作副本的 .command/.sh 必须带可执行位，否则双击报无访问权限。

    发行 ZIP 由 public_release.build 强制写 0755；Git 检出只看索引模式，
    这里锁定索引，防止入口脚本再次以 100644 提交。
    """
    root = Path(__file__).resolve().parents[1]
    if not (root / '.git').exists():
        pytest.skip('发行 ZIP 安装没有 Git 索引；包内模式由 manifest 记录')
    listed = subprocess.run(['git', 'ls-files', '-s', '-z'], cwd=root, capture_output=True,
                            text=True, check=True).stdout
    modes = {}
    for row in listed.split('\0'):
        if row:
            meta, name = row.split('\t', 1)
            modes[name] = meta.split()[0]
    declared = [n for n in json.loads((root / 'PUBLIC_FILES.json').read_text(encoding='utf-8'))['files']
                if n.endswith(('.command', '.sh'))]
    assert declared, '白名单应包含 macOS 入口脚本'
    assert {n: modes.get(n) for n in declared} == dict.fromkeys(declared, '100755')


@pytest.mark.real_probe
@pytest.mark.skipif(sys.platform == 'linux', reason='Linux 主机无需探测记录即放行')
def test_probe_record_is_required_and_fingerprint_bound(project_root, monkeypatch):
    """require_probe 必须在缺记录或指纹不符时拒绝，合法记录才放行。

    测试默认替换掉探测入口；这条用例显式使用真实实现，避免安全语义
    只依赖 native 验收。
    """
    from native_dist import doctor
    with pytest.raises(RuntimeError, match='行为验证'):
        doctor.require_probe(project_root)
    # 临时项目根没有应用源码和依赖清单，指纹无从计算；先显式指向源码根。
    source = Path(__file__).resolve().parents[1]
    monkeypatch.setenv('MULTIOMICS_APP_ROOT', str(source))
    shutil.copy(source / 'requirements.txt', project_root / 'requirements.txt')
    (project_root / 'native_dist').mkdir(exist_ok=True)
    shutil.copy(source / 'native_dist/requirements-native.txt',
                project_root / 'native_dist/requirements-native.txt')
    record = {'status': 'passed', 'platform': sys.platform, 'machine': platform.machine(),
              'guard_fingerprint': doctor.guard_fingerprint(project_root),
              'runtime_fingerprint': doctor.runtime_fingerprint(project_root)}
    (project_root / '.runtime').mkdir(exist_ok=True)
    (project_root / '.runtime/doctor.json').write_text(json.dumps(record), encoding='utf-8')
    doctor.require_probe(project_root)
    # 任一指纹不符都必须重新拒绝，而不是复用旧记录。
    record['runtime_fingerprint'] = '0' * 64
    (project_root / '.runtime/doctor.json').write_text(json.dumps(record), encoding='utf-8')
    with pytest.raises(RuntimeError, match='行为验证'):
        doctor.require_probe(project_root)


def test_v8_resources_and_dependencies():
    from native_dist.public_release import source_files
    root = Path(__file__).resolve().parents[1]
    names = {n for n, _, _ in source_files(root)}
    # Bioconductor package list and the R-environment discovery module ship.
    assert 'native_dist/r-bioc-packages.txt' in names
    assert 'multiomics_tui/r_env.py' in names
    assert 'docs/INSTALL_UPDATE_zh.md' in names
    # Earlier-generation guides are not delivered in the v8 package.
    assert 'native_dist/USER_GUIDE_v7_zh.md' not in names
    assert not any(n.startswith(('install.log/', 'reports/', 'analysis/')) for n in names)


def test_r_env_lists_installed_packages(tmp_path, monkeypatch):
    from multiomics_tui import r_env
    monkeypatch.setattr(r_env, 'SYSTEM_LIBRARIES', ())
    library = tmp_path / 'library'
    for name in ('GSVA', 'dplyr', 'survival'):
        folder = library / name
        folder.mkdir(parents=True)
        (folder / 'DESCRIPTION').write_text('Package: ' + name + '\nVersion: 1\n')
    (library / 'not_a_package.txt').write_text('ignore me')
    cfg = {'runtime': {'r_library_paths': [str(library)]}}
    packages = r_env.available_packages(cfg)
    assert packages == ['GSVA', 'dplyr', 'survival']
    note = r_env.availability_note(cfg)
    assert 'GSVA' in note and 'ONLY these packages' in note


def test_r_env_note_empty_without_libraries(tmp_path, monkeypatch):
    from multiomics_tui import r_env
    monkeypatch.setattr(r_env, 'SYSTEM_LIBRARIES', ())
    assert r_env.availability_note({'runtime': {'r_library_paths': [str(tmp_path / 'missing')]}}) == ''


def test_missing_dependency_is_correctable_not_policy():
    from multiomics_tui import task_runner
    from orchestrator.llm_client import LLMNonRetryableValidationError
    assert issubclass(task_runner.MissingDependencyError, task_runner.GeneratedCodeError)
    assert issubclass(task_runner.MissingDependencyError, ImportError)
    # It must NOT be the non-retryable policy error that aborts a task.
    assert not issubclass(task_runner.MissingDependencyError, LLMNonRetryableValidationError)


def test_codegen_prompt_injects_available_packages(tmp_path):
    from multiomics_tui import task_runner
    library = tmp_path / 'library'
    folder = library / 'GSVA'
    folder.mkdir(parents=True)
    (folder / 'DESCRIPTION').write_text('Package: GSVA\nVersion: 1\n')
    cfg = {'runtime': {'r_library_paths': [str(library)]}}
    seen = {}

    class Client:
        def chat_json(self, role, system, user, schema_name, validator=None):
            seen['system'] = system
            step = json.loads(user)['step']
            parsed = {'files': [{'step_id': step['id'], 'content': "print('ok')\n"}],
                      'output_contracts': [{'path': p, 'structure': 'CSV columns a; one row'} for p in step['outputs']]}
            validator(parsed)
            return {'parsed': parsed}

    plan = {'steps': [{'id': 's1', 'language': 'python', 'purpose': 'x', 'outputs': ['tables/a.csv']},
                      {'id': 's2', 'language': 'python', 'purpose': 'y', 'outputs': ['tables/b.csv']}]}
    task_runner._codegen(Client(), plan, cfg=cfg)
    assert 'GSVA' in seen['system'] and 'do not install anything' in seen['system']


def test_macos_guard_has_macos26_authorizations():
    """D2-D7 fixes are part of the v8 source, not a receiving-machine patch."""
    from multiomics_tui import macos_guard
    source = Path(macos_guard.__file__).read_text(encoding='utf-8')
    assert 'kern.usrstack64' in source and 'kern.hostname' in source
    assert 'multiomics.uname-write-noop' in source
    assert '/private/var/select/sh' in source
    assert '(allow ipc-posix-shm)' in source
    assert 'com.apple.securityd' in source
    # External Homebrew libraries are discovered, never hardcoded to one prefix.
    assert 'linked_external_library_dirs' in source
    policy = macos_guard.profile(Path(macos_guard.__file__).resolve().parent,
                                 [], [], [])
    assert 'kern.usrstack64' in policy and '/private/var/select/sh' in policy
    assert 'ipc-posix-shm' in policy and 'com.apple.securityd' in policy


def test_macos_guard_grants_task_ancestor_metadata(tmp_path):
    """D7: realpath()/normalizePath(mustWork=TRUE) needs ancestor metadata.

    GSVA's HDF5Array dependency canonicalizes tempdir paths; without ancestor
    metadata grants the sandbox returns EPERM and GSVA fails to load.
    """
    from multiomics_tui import macos_guard
    task = tmp_path / 'analysis' / 'task_v8'
    task.mkdir(parents=True)
    policy = macos_guard.profile(task, [], [], [])
    for ancestor in task.parents:
        if str(ancestor) == '/':
            continue
        assert f'(allow file-read-metadata (literal {json.dumps(str(ancestor))}))' in policy
    # Ancestor metadata must never become recursive data access.
    assert f'(allow file-read* (subpath {json.dumps(str(task.parents[0]))}))' not in policy


def test_v8_acceptance_module_and_diagnostics_wiring():
    import native_dist.diagnostics as diagnostics
    source = Path(diagnostics.__file__).read_text(encoding='utf-8')
    assert 'native_dist.v8_acceptance' in source
    from native_dist import v8_acceptance
    assert hasattr(v8_acceptance, 'acceptance') and hasattr(v8_acceptance, 'main')
    # The GSVA probe must work with both the legacy and the param-object API.
    probe = Path(v8_acceptance.__file__).read_text(encoding='utf-8')
    assert 'plageParam' in probe and 'method="plage"' in probe

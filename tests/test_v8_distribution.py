"""Portable v8 regressions: dependency awareness and macOS sandbox defaults.

These run on any platform; native enforcement (real Seatbelt/R) needs macOS.
"""
import json
from pathlib import Path

import pytest


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

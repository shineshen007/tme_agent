import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from multiomics_tui import planning_evidence as pe
from multiomics_tui import task_environment as te
from multiomics_tui.task_sessions import canonical_hash
from planning_fixtures import DOC, Literature, binding, proposal


@pytest.fixture
def pending_plan(tmp_path):
    data = tmp_path / 'data/raw/input.csv'
    data.parent.mkdir(parents=True)
    data.write_text('x\n1\n')
    inspected = {'profiles': [{'path': 'data/raw/input.csv', 'sha256': te.digest(data),
                              'object_class': 'data.frame', 'keys': {}, 'columns': {}}],
                 'apis': {}, 'software': {}}
    proposed = proposal()
    proposed['candidates'][0].update(package='six', apis=['six.ensure_str'])
    def chat(*args, **kwargs):
        kwargs['validator'](proposed)
        return {'parsed': copy.deepcopy(proposed)}
    def probe(*args):
        return [{'id': 'finite', 'language': 'python', 'package': 'six',
                 'runtime': '.'.join(map(str, sys.version_info[:3])), 'status': 'missing'}]
    def transport(*args, **kwargs):
        return json.dumps({'info': {'name': 'six', 'version': '1.17.0',
                                   'summary': DOC, 'project_urls': {}}}).encode()
    receipt = pe.collect_planning_evidence(SimpleNamespace(chat_json=chat, private_ids=()), tmp_path,
        'patient continuous independent', inspected, {}, probe=probe, software_transport=transport,
        literature_service=Literature())
    assert receipt['protocol'] == pe.INSTALL_PROTOCOL
    plan = {'goal': 'patient continuous independent', 'analysis_unit': 'patient', 'method': 'fixture',
        'inputs': [{'path': 'data/raw/input.csv', 'sha256': te.digest(data)}],
        'steps': [{'id': 'check', 'language': 'python', 'method_ids': ['finite'], 'outputs': ['tables/out.csv']}],
        'method_bindings': [binding()], 'planning_protocol': pe.INSTALL_PROTOCOL,
        'planning_evidence': {k: v for k, v in receipt.items() if k != 'bundle'},
        'parameters': [{'name': 'planning_protocol', 'value': pe.INSTALL_PROTOCOL,
                        'source': 'evidence:' + receipt['sha256']}], 'limitations': []}
    plan['environment_spec'] = te.specification(receipt['bundle'], plan['method_bindings'])
    return tmp_path, plan


class FakeInstaller:
    calls = 0
    def __init__(self, root, folder, cfg, progress):
        self.folder = folder
        (folder / 'runtime').mkdir(exist_ok=True)
    def python(self, dependencies):
        type(self).calls += 1
        (self.folder / 'runtime/package.txt').write_text('installed fixture')
        (self.folder / 'requirements.lock').write_text('six==1.17.0')
        executable = self.folder / 'runtime/python' / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b'fixture interpreter')
        return str(executable), {'requirements.lock': te.digest(self.folder / 'requirements.lock')}


def installed_probe(*args):
    return [{'id': 'finite', 'language': 'python', 'package': 'six', 'status': 'verified',
             'version': '1.17.0', 'runtime': '.'.join(map(str, sys.version_info[:3])), 'interpreter': args[2]['runtime']['task_python'], 'apis': {}}]


def test_missing_package_can_be_approved_without_install(pending_plan):
    root, plan = pending_plan
    pe.verify_planning_evidence(root, plan, stage='approve', cfg={})
    assert not (root / '.runtime/task_environments').exists()


def test_prepare_reuse_and_integrity(pending_plan):
    root, plan = pending_plan
    FakeInstaller.calls = 0
    first = te.prepare(root, plan, canonical_hash(plan), {}, installer=FakeInstaller, probe=installed_probe)
    assert te.prepare(root, plan, canonical_hash(plan), {}, installer=FakeInstaller, probe=installed_probe) == first
    assert FakeInstaller.calls == 1
    (root / first['directory'] / 'runtime/package.txt').write_text('changed')
    with pytest.raises(te.EnvironmentError, match='changed'):
        te.verify_receipt(root, plan, first)


def test_failed_api_never_publishes_receipt(pending_plan):
    root, plan = pending_plan
    with pytest.raises(te.EnvironmentError, match='API verification'):
        te.prepare(root, plan, canonical_hash(plan), {}, installer=FakeInstaller, probe=lambda *a: [])
    assert not list((root / '.runtime/task_environments').rglob('receipt.json'))
    assert json.loads(next((root / '.runtime/task_environments').rglob('transaction.json')).read_text())['status'] == 'failed'


def test_changed_spec_refused(pending_plan):
    root, plan = pending_plan
    plan['environment_spec']['dependencies'][0]['version'] = '1.16.0'
    with pytest.raises(pe.PlanningEvidenceError, match='specification'):
        pe.verify_planning_evidence(root, plan, stage='approve', cfg={})


def test_unapproved_install_refused(pending_plan):
    root, plan = pending_plan
    with pytest.raises(ValueError, match='approved plan'):
        te.prepare(root, plan, 'wrong', {}, installer=FakeInstaller, probe=installed_probe)
    assert not (root / '.runtime/task_environments').exists()


def test_new_plan_validation_preserves_environment(pending_plan):
    from multiomics_tui.task_planner import validate_plan, render_plan
    root, plan = pending_plan
    plan['steps'][0]['purpose'] = 'Check input values'
    raw = {**plan, 'inputs': [i['path'] for i in plan['inputs']]}
    validated = validate_plan(root, raw, plan['goal'], {})
    assert validated['environment_spec'] == plan['environment_spec']
    assert 'Install and verify APIs after approval' in render_plan('task', validated, canonical_hash(validated))


def test_changed_lock_refused(pending_plan):
    root, plan = pending_plan
    receipt = te.prepare(root, plan, canonical_hash(plan), {}, installer=FakeInstaller, probe=installed_probe)
    (root / receipt['directory'] / 'requirements.lock').write_text('six==0.0.1')
    with pytest.raises(te.EnvironmentError, match='lock changed'):
        te.verify_receipt(root, plan, receipt)


def test_failure_retries_without_overwriting_attempt(pending_plan):
    root, plan = pending_plan
    with pytest.raises(te.EnvironmentError):
        te.prepare(root, plan, canonical_hash(plan), {}, installer=FakeInstaller, probe=lambda *a: [])
    before = list((root / '.runtime/task_environments').glob('*/attempt_*'))
    receipt = te.prepare(root, plan, canonical_hash(plan), {}, installer=FakeInstaller, probe=installed_probe)
    assert root / receipt['directory'] != before[0]
    assert json.loads((before[0] / 'transaction.json').read_text())['status'] == 'failed'


def test_installed_bytecode_is_integrity_bound(pending_plan):
    root, plan = pending_plan
    receipt = te.prepare(root, plan, canonical_hash(plan), {}, installer=FakeInstaller, probe=installed_probe)
    cache = root / receipt['directory'] / 'runtime/__pycache__'
    cache.mkdir()
    (cache / 'changed.pyc').write_bytes(b'not approved')
    with pytest.raises(te.EnvironmentError, match='changed'):
        te.verify_receipt(root, plan, receipt)


def test_same_environment_cannot_be_installed_concurrently(pending_plan):
    root, plan = pending_plan
    class RecursiveInstaller(FakeInstaller):
        def python(self, dependencies):
            with pytest.raises(OSError):
                te.prepare(root, plan, canonical_hash(plan), {}, installer=FakeInstaller, probe=installed_probe)
            return super().python(dependencies)
    te.prepare(root, plan, canonical_hash(plan), {}, installer=RecursiveInstaller, probe=installed_probe)


def test_author_repository_requires_independent_link():
    candidate = {'id': 'example', 'language': 'python', 'package': 'example',
                 'repository_url': 'https://github.com/imposter/example', 'apis': ['example.run']}
    def transport(url, **kwargs):
        return json.dumps({'info': {'name': 'example', 'version': '1.0', 'summary': DOC,
                                   'project_urls': {'Repository': 'https://github.com/author/example'}}}).encode()
    with pytest.raises(pe.PlanningEvidenceError, match='not linked'):
        pe.retrieve_software_sources(candidate, {'status': 'missing'}, {}, transport)


def test_author_commit_is_resolved_from_verified_link():
    candidate = {'id': 'example', 'language': 'python', 'package': 'example',
                 'repository_url': 'https://github.com/author/example', 'apis': ['example.run']}
    def transport(url, **kwargs):
        if 'pypi.org' in url:
            return json.dumps({'info': {'name': 'example', 'version': '1.0', 'summary': DOC,
                'project_urls': {'Repository': candidate['repository_url']}}}).encode()
        if 'api.github.com' in url:
            return json.dumps({'sha': 'a' * 40}).encode()
        return b'[project]\nname="example"\nversion="1.1.0"\n'
    source = pe.retrieve_software_sources(candidate, {'status': 'missing'}, {}, transport)
    assert source['install_source']['commit'] == 'a' * 40
    assert source['version'] == '1.1.0'
    assert source['verification_status'] == 'source_verified_api_pending'


def test_offline_missing_environment_never_starts_installer(pending_plan):
    root, plan = pending_plan
    FakeInstaller.calls = 0
    with pytest.raises(te.EnvironmentError, match='Offline mode'):
        te.prepare(root, plan, canonical_hash(plan), {'planning': {'evidence': {'offline': True}}},
                   installer=FakeInstaller, probe=installed_probe)
    assert FakeInstaller.calls == 0


def test_offline_ready_environment_is_reusable(pending_plan):
    root, plan = pending_plan
    first = te.prepare(root, plan, canonical_hash(plan), {}, installer=FakeInstaller, probe=installed_probe)
    assert te.prepare(root, plan, canonical_hash(plan), {'planning': {'evidence': {'offline': True}}},
                      installer=FakeInstaller, probe=installed_probe) == first


def test_environment_retry_preserves_approval_and_does_not_replan(project_root, stub_cfg, monkeypatch):
    from test_task_lifecycle import make_session, prepare
    from planning_fixtures import environment
    monkeypatch.setattr(pe, 'probe_method_apis', environment)
    from multiomics_tui import controller
    session, store, state = make_session(project_root, stub_cfg)
    prepare(session, monkeypatch)
    state.update(status='blocked', approved_hash=state['plan_hash'], current_stage='environment:downloading_packages')
    store.save(state)
    calls = []
    monkeypatch.setattr(controller, 'generate_plan', lambda *a, **k: pytest.fail('Must not replan an approved installation retry'))
    monkeypatch.setattr(session.jobs, 'start', lambda *a, **k: calls.append(k) or {
        'job_id': 'environment_retry', 'status': 'running', 'log_path': 'workspace/tui/logs/environment_retry.log'})
    response = session.handle('/retry ' + state['task_id'])
    assert response.kind == 'job' and len(calls) == 1, response.message
    current = store.get(state['task_id'])
    assert current['plan_version'] == state['plan_version']
    assert current['approved_hash'] == state['plan_hash']


def test_author_python_build_uses_static_dependencies_without_shadowing_path(tmp_path):
    folder = tmp_path / 'stage'
    folder.mkdir()
    source = folder / 'source'
    source.mkdir()
    (source / 'pyproject.toml').write_text('[project]\nname="example"\nversion="1.0"\ndependencies=[]\n[build-system]\nrequires=["setuptools"]\nbuild-backend="setuptools.build_meta"\n')
    tools = folder / 'tools' / ('Scripts' if sys.platform == 'win32' else 'bin')
    tools.mkdir(parents=True)
    (tools / ('python.exe' if sys.platform == 'win32' else 'python')).touch()
    (tools / ('uv.exe' if sys.platform == 'win32' else 'uv')).touch()
    backend = te.Installer.__new__(te.Installer)
    backend.folder = folder
    backend.runtime = folder / 'runtime'
    backend.env = {'MULTIOMICS_RUNTIME_ROOT': str(tmp_path)}
    backend.source_tree = lambda dependency: source
    stages = []
    def run(argv, stage, guarded=False):
        stages.append((stage, guarded))
        if stage == 'resolving_dependencies':
            (folder / 'requirements.lock').write_text('packaging==26.0\nsetuptools==80.0.0\n')
        elif stage == 'building_author_package':
            output = Path(argv[argv.index('--out-dir') + 1])
            (output / 'example-1.0-py3-none-any.whl').touch()
    backend.run = run
    backend.python([{'package':'example','version':'1.0','source':{'kind':'author_repository'}}])
    assert ('building_author_package', True) in stages
    assert ('installing_author_package', True) in stages
    assert (folder / 'requirements.in').read_text().splitlines() == ['setuptools', 'packaging']


def test_artifact_download_retries_and_checks_hash(tmp_path, monkeypatch):
    import hashlib
    import socket
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('151.101.0.223', 443))])
    backend = te.Installer.__new__(te.Installer)
    target = tmp_path / 'package.whl'
    calls = []
    def run(argv, stage):
        calls.append(argv)
        partial = target.with_name(target.name + '.partial')
        if len(calls) == 1:
            partial.write_bytes(b'pa')
            raise te.EnvironmentError('temporary network failure')
        assert partial.read_bytes() == b'pa'
        partial.write_bytes(b'package')
    backend.run = run
    monkeypatch.setattr(te.shutil, 'which', lambda name: '/usr/bin/curl')
    expected = hashlib.sha256(b'package').hexdigest()
    assert backend.download_artifact('https://files.pythonhosted.org/package.whl', target,
                                     expected_sha256=expected, expected_size=7) == expected
    assert len(calls) == 2 and '--continue-at' in calls[1]
    target.write_bytes(b'tampered')
    with pytest.raises(te.EnvironmentError, match='checksum'):
        backend.download_artifact('https://files.pythonhosted.org/package.whl', target,
                                  expected_sha256=expected)


def test_artifact_download_rejects_unapproved_host(tmp_path):
    backend = te.Installer.__new__(te.Installer)
    with pytest.raises(te.EnvironmentError, match='outside approved'):
        backend.download_artifact('https://example.org/package.whl', tmp_path / 'package.whl')


def test_r_rejects_lock_source_before_package_download(tmp_path, monkeypatch):
    from multiomics_tui import actions
    backend = te.Installer.__new__(te.Installer)
    backend.root = backend.folder = tmp_path
    backend.runtime = tmp_path / 'runtime'
    backend.runtime.mkdir()
    (tmp_path / 'r-tools/pak').mkdir(parents=True)
    backend.env = {}
    calls = []
    def run(argv, stage, guarded=False):
        calls.append((argv, stage))
        assert stage == 'resolving_dependencies'
        (tmp_path / 'r-packages.lock').write_text(json.dumps({'url':'https://unapproved.example/package.tar.gz'}))
    backend.run = run
    monkeypatch.setattr(actions, 'rscript_path', lambda root: 'Rscript')
    monkeypatch.setattr(te.shutil, 'which', lambda name: '/opt/R/bin/Rscript')
    with pytest.raises(te.EnvironmentError, match='unapproved source'):
        backend.r([{'package':'crayon', 'version':'1.5.3', 'source':{'kind':'cran'}}])
    assert len(calls) == 1
    assert calls[0][0][0] == '/opt/R/bin/Rscript'


@pytest.mark.parametrize('field,value', [('python', sys.executable), ('python', None), ('r_library', '/shared/R')])
def test_receipt_runtime_redirect_refused(pending_plan, field, value):
    root, plan = pending_plan
    receipt = te.prepare(root, plan, canonical_hash(plan), {}, installer=FakeInstaller, probe=installed_probe)
    receipt[field] = value
    with pytest.raises(te.EnvironmentError, match='binding'):
        te.verify_receipt(root, plan, receipt)


@pytest.mark.skipif(sys.platform != 'darwin', reason='Native macOS installer process isolation')
def test_native_installer_reaps_children_but_cannot_signal_controller(tmp_path):
    import os
    folder = tmp_path / 'attempt'
    folder.mkdir()
    backend = te.Installer(tmp_path, folder, {}, lambda stage: None)
    script = folder / 'worker_cleanup.py'
    script.write_text(
        'import os, subprocess, sys\n'
        'child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])\n'
        'child.terminate()\n'
        'child.wait(timeout=5)\n'
        'try:\n'
        f'    os.kill({os.getpid()}, 0)\n'
        'except PermissionError:\n'
        '    pass\n'
        'else:\n'
        '    raise AssertionError("Installer can signal controller")\n')
    backend.run([sys.executable, script], 'verifying_worker_cleanup', True)


def test_r_downloads_only_frozen_registry_artifacts(tmp_path, monkeypatch):
    import hashlib
    from multiomics_tui import actions
    backend = te.Installer.__new__(te.Installer)
    backend.root = backend.folder = tmp_path
    backend.runtime = tmp_path / 'runtime'
    backend.runtime.mkdir()
    (tmp_path / 'r-tools/pak').mkdir(parents=True)
    backend.env = {}
    stages = []
    checksum = hashlib.sha256(b'fixture').hexdigest()
    def run(argv, stage, guarded=False):
        stages.append((stage, guarded))
        if stage == 'resolving_dependencies':
            (tmp_path / 'r-packages.lock').write_text(json.dumps({'packages':[
                {'ref':'digest','type':'standard','package':'digest','version':'0.6.39','sha256':'\n  ' + checksum, 'target':'digest.tar.gz'},
                {'type':'local','package':'authorpkg','version':'1.0','sources':[str(tmp_path/'author')]}
            ]}))
        if stage == 'downloading_packages':
            (tmp_path/'r-downloads/digest.tar.gz').write_bytes(b'fixture')
    backend.run = run
    monkeypatch.setattr(actions, 'rscript_path', lambda root: 'Rscript')
    monkeypatch.setattr(te.shutil, 'which', lambda name: '/opt/R/bin/Rscript')
    backend.r([{'package':'digest','version':'0.6.39','source':{'kind':'cran'}}])
    script = (tmp_path / 'download_packages.R').read_text()
    assert 'digest@0.6.39' in script and 'authorpkg' not in script and 'dependencies=FALSE' in script
    assert stages[-1] == ('installing_packages', True)
    assert json.loads((tmp_path/'r-packages.lock').read_text())['packages'][0]['sha256'] == checksum
    assert json.loads((tmp_path/'r-resolved.lock').read_text())['packages'][0]['sha256'].startswith('\n')
    assert json.loads((tmp_path/'r-install.lock').read_text())['packages'][0]['ref'] == 'local::' + str(tmp_path/'r-downloads/digest.tar.gz')


def test_bioc_missing_package_uses_released_repository_for_existing_r():
    import gzip
    urls = []
    def transport(url, **kwargs):
        urls.append(url)
        if url.endswith('config.yaml'):
            return b'release_version: "3.23"\nr_ver_for_bioc_ver: {"3.21": "4.5", "3.22": "4.5", "3.23": "4.6", "3.24": "4.5"}\n'
        if 'cran.r-project.org' in url:
            return gzip.compress(b'Package: unrelated\nVersion: 1.0\n\n')
        if url.endswith('PACKAGES.gz'):
            assert '/3.22/' in url
            return gzip.compress(b'Package: BiocGenerics\nVersion: 0.56.0\n\n')
        assert '/3.22/bioc/html/BiocGenerics.html' in url
        return b'<html>Generic functions for Bioconductor packages.</html>'
    source = pe.retrieve_software_sources({'language':'r','package':'BiocGenerics','apis':['combine']},
        {'status':'missing','runtime':'4.5.2'}, {}, transport)
    assert '/3.22/' in source['registry_url'] and source['version'] == '0.56.0'
    assert any(u.endswith('config.yaml') for u in urls)


def test_source_http_detects_truncation_and_decodes_gzip(monkeypatch):
    import io,gzip
    class ShortRead(io.BytesIO):
        def read(self, count=-1):
            return super().read(min(count, 3))
    monkeypatch.setattr(pe, '_url', lambda *a, **k: None)
    payload = gzip.compress(b'{"verified":true}')
    response = ShortRead(payload)
    response.url = 'https://pypi.org/fixture'
    response.headers = {'Content-Length': str(len(payload)), 'Content-Encoding':'gzip'}
    monkeypatch.setattr(pe.urllib.request, 'build_opener', lambda *a: SimpleNamespace(open=lambda *a, **k: response))
    assert pe._http(response.url, {'pypi.org'}, 10) == b'{"verified":true}'
    response = io.BytesIO(b'{"partial')
    response.url = 'https://pypi.org/fixture'
    response.headers = {'Content-Length':'100'}
    with pytest.raises(ConnectionError, match='truncated'):
        pe._http(response.url, {'pypi.org'}, 10)


@pytest.mark.skipif(sys.platform == 'win32', reason='Symlink creation needs Windows privilege')
def test_inventory_binds_internal_directory_alias_and_rejects_escape(tmp_path):
    tree=tmp_path/'runtime';tree.mkdir()
    (tree/'lib').mkdir();(tree/'lib/package.py').write_text('value=1')
    (tree/'lib64').symlink_to('lib',target_is_directory=True)
    original=te.inventory(tree)
    assert 'lib64' in original and 'lib/package.py' in original
    (tree/'lib/package.py').write_text('value=2')
    assert original != te.inventory(tree)
    (tree/'outside').symlink_to(tmp_path,target_is_directory=True)
    with pytest.raises(te.EnvironmentError,match='Linked environment directory'):
        te.inventory(tree)


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX sandbox paths')
def test_linux_installer_preserves_lexical_runtime_mount(tmp_path, monkeypatch):
    from multiomics_tui import installer_guard
    stage=tmp_path/'stage';stage.mkdir()
    runtime=tmp_path/'actual';runtime.mkdir()
    alias=tmp_path/'alias';alias.symlink_to(runtime,target_is_directory=True)
    monkeypatch.setattr(installer_guard.shutil,'which',lambda name:'/usr/bin/bwrap')
    args=installer_guard.command(stage,['/fixture/python'],{'TASK_ALLOWED_READS':json.dumps([str(alias)])})
    mounts=[args[i+1:i+3] for i,value in enumerate(args) if value=='--ro-bind']
    assert [str(alias),str(alias)] in mounts
    assert '--unshare-all' in args
    with pytest.raises(ValueError,match='parent'):
        installer_guard.command(stage,['/fixture/python'],{'TASK_ALLOWED_READS':json.dumps([str(tmp_path)])})

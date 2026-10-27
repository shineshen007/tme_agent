"""Exercise real release ZIPs and HTTP downloads without GitHub or private inputs."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import stat
import threading
from zipfile import ZipFile, ZipInfo

import pytest

from native_dist import public_release as release
from native_dist import public_update as updater
from native_dist.public_config import initialize

SOURCE = Path(__file__).resolve().parents[1]

# 版本从 VERSION 派生：夹具模拟"当前版本 → 下一个补丁版本"的升级，
# 避免每次发版都要改测试里的字面量。
BASE_VERSION = (SOURCE / 'VERSION').read_text(encoding='utf-8').strip()
_MAJOR, _MINOR, _PATCH = (int(part) for part in BASE_VERSION.split('.'))
NEXT_VERSION = f'{_MAJOR}.{_MINOR}.{_PATCH + 1}'


@pytest.fixture(scope='session')
def release_assets(tmp_path_factory):
    """Build the two release ZIPs once: each carries the ~32 MB built-in dataset.

    Building per test would recompress the dataset for every case, so the
    archives are session-scoped and every test unpacks its own copy. Nothing
    writes into the release directories afterwards.
    """
    base = tmp_path_factory.mktemp('releases')
    source = base / 'source'
    source.mkdir()
    for name, _, data in release.source_files(SOURCE):
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    first = release.build(source, base / 'releases-v0')
    (source / 'VERSION').write_text(NEXT_VERSION + '\n')
    (source / 'README.md').write_text((source / 'README.md').read_text(encoding='utf-8') + '\nPatch fixture.\n', encoding='utf-8')
    names = json.loads((source / 'PUBLIC_FILES.json').read_text())['files']
    removed = 'examples/synthetic_samples.csv'
    names.remove(removed)
    added = 'docs/new_release_fixture.md'
    names.append(added)
    (source / added).write_text('New release fixture.\n')
    (source / 'PUBLIC_FILES.json').write_text(json.dumps({'files': sorted(names)}))
    second = release.build(source, base / 'releases-v1')
    return first, second


@pytest.fixture
def releases(tmp_path, release_assets):
    first, second = release_assets
    root = tmp_path / 'installed'
    updater.unpack(Path(first['archive']), Path(first['manifest']), root)
    initialize(root)
    protected = {'data/raw/private.csv': b'private synthetic sentinel\n',
                 'analysis/previous/result.csv': b'preserved result\n',
                 'config/api.local.yaml': b'llm: {}\n',
                 '.venv/keep.txt': b'existing environment\n'}
    for name, data in protected.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    protected['config/study_config.yaml'] = (root / 'config/study_config.yaml').read_bytes()
    return root, first, second, protected


def assert_protected(root, protected):
    assert {name: (root / name).read_bytes() for name in protected} == protected


@contextmanager
def server(record, *, corrupt=False, missing=False, prerelease=False):
    v = record['version']
    folder = Path(record['archive']).parent
    names = [f'tme_agent-v{v}.zip', f'manifest-v{v}.json', f'SHA256SUMS-v{v}.txt']
    bodies = {name: (folder / name).read_bytes() for name in names}
    if corrupt:
        bodies[names[0]] += b'corrupted'
    httpd = None

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith('/repos/'):
                info = {'tag_name': 'v' + v, 'draft': False, 'prerelease': prerelease,
                        'assets': [{'name': name,
                                    'browser_download_url': f'http://127.0.0.1:{httpd.server_port}/{name}',
                                    'digest': 'sha256:' + hashlib.sha256((folder / name).read_bytes()).hexdigest()}
                                   for name in names if not (missing and name.endswith('.zip'))]}
                data = json.dumps(info).encode()
            else:
                data = bodies.get(self.path.removeprefix('/'))
                if data is None:
                    self.send_error(404)
                    return
            self.send_response(200)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_):
            pass

    httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield updater.ReleaseClient(f'http://127.0.0.1:{httpd.server_port}', local_testing=True)
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def environment(_):
    return {'status': 'passed', 'diagnostic': 'fixture'}


def test_public_archive_contains_only_declared_source_and_license(releases):
    root, first, _, _ = releases
    manifest = updater.installed(root)
    assert manifest['version'] == BASE_VERSION
    assert manifest['source_data'] == []
    assert manifest['builtin_datasets'] == ['TCGA-LIHC']
    assert manifest['license'] == 'GPL-3.0-or-later'
    with ZipFile(first['archive']) as z:
        names = [s.removeprefix('tme_agent/') for s in z.namelist()]
    assert set(names) == {s['path'] for s in manifest['files']} | {release.MANIFEST, release.SUMS}
    # Private study data and local state never ship; the built-in open-access
    # dataset does, and only under datasets/.
    assert not any(s.startswith(('data/', 'knowledge/', 'analysis/', '.runtime/', '.venv/')) for s in names)
    assert {s for s in names if s.startswith('datasets/')} == {
        s['path'] for s in manifest['files'] if s['path'].startswith('datasets/')}
    assert 'datasets/TCGA-LIHC/dataset.json' in names
    assert 'datasets/TCGA-LIHC/expression/counts_paired.rds' in names
    assert 'datasets/TCGA-LIHC/expression/gene_annotation.csv' in names
    # The shipped dataset records its provenance, so the release gate is clear.
    assert release.pending_provenance(root) == []


def test_public_archive_marks_entry_scripts_executable(releases):
    """ZIP 内 .command/.sh 必须是 0755，且与 manifest 记录一致。

    解压后双击 setup/start 入口依赖该模式；updater 解包时按 manifest 的
    mode 字段恢复权限，所以两处必须同源。
    """
    _, first, _, _ = releases
    with ZipFile(first['archive']) as z:
        modes = {i.filename.removeprefix('tme_agent/'): i.external_attr >> 16 for i in z.infolist()}
    scripts = {n: stat.S_IMODE(m) for n, m in modes.items() if n.endswith(('.command', '.sh'))}
    assert scripts and set(scripts.values()) == {0o755}
    manifest = json.loads(Path(first['manifest']).read_text(encoding='utf-8'))
    recorded = {item['path']: item['mode'] for item in manifest['files']
                if item['path'].endswith(('.command', '.sh'))}
    # 与白名单同源：每个声明的 .command/.sh 都要在包内且为 0755。
    declared = {n for n in json.loads((SOURCE / 'PUBLIC_FILES.json').read_text(encoding='utf-8'))['files']
                if n.endswith(('.command', '.sh'))}
    assert set(recorded) == declared and set(recorded.values()) == {0o755}


def test_check_is_read_only_and_upgrade_repeat_rollback(releases):
    root, _, second, protected = releases
    snapshot = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    with server(second) as client:
        assert updater.update(root, check=True, client=client)['status'] == 'update_available'
        assert snapshot == {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()}
        result = updater.update(root, tag='v' + NEXT_VERSION, client=client, environment=environment)
        assert result['status'] == 'updated'
        assert result['phases'] == {'code': 'passed', 'environment': 'passed', 'diagnostic': 'passed'}
        assert updater.installed(root)['version'] == NEXT_VERSION
        assert not (root / 'examples/synthetic_samples.csv').exists()
        assert (root / 'docs/new_release_fixture.md').exists()
        assert updater.update(root, client=client, environment=environment)['status'] == 'already_updated'
    assert_protected(root, protected)
    assert updater.rollback(root)['status'] == 'rolled_back'
    assert updater.installed(root)['version'] == BASE_VERSION
    assert (root / 'examples/synthetic_samples.csv').exists()
    assert not (root / 'docs/new_release_fixture.md').exists()
    assert_protected(root, protected)
    with server(second) as client:
        assert updater.update(root, client=client, environment=environment)['status'] == 'updated'


@pytest.mark.parametrize('name', ['README.md', 'config/study_config.example.yaml'])
def test_modified_source_refused(releases, name):
    root, _, second, protected = releases
    target = root / name
    target.write_text('local edit\n')
    with server(second) as client, pytest.raises(ValueError, match='已修改'):
        updater.update(root, client=client, environment=environment)
    assert target.read_text() == 'local edit\n'
    assert_protected(root, protected)


@pytest.mark.parametrize('status', sorted(updater.ACTIVE))
def test_active_job_refused(releases, status):
    root, _, second, protected = releases
    jobs = root / 'workspace/tui/jobs'
    jobs.mkdir(parents=True)
    (jobs / 'active.json').write_text(json.dumps({'status': status}))
    with server(second) as client, pytest.raises(RuntimeError, match='活动任务'):
        updater.update(root, client=client, environment=environment)
    assert updater.installed(root)['version'] == BASE_VERSION
    assert_protected(root, protected)


def test_maintenance_lock_refused(releases):
    root, _, second, _ = releases
    with updater.maintenance(root), server(second) as client, pytest.raises(RuntimeError, match='另一更新'):
        updater.update(root, client=client, environment=environment)


@pytest.mark.parametrize('options', [{'corrupt': True}, {'missing': True}, {'prerelease': True}])
def test_bad_download_never_changes_source(releases, options):
    root, _, second, protected = releases
    old = (root / release.MANIFEST).read_bytes()
    with server(second, **options) as client, pytest.raises(ValueError):
        updater.update(root, client=client, environment=environment)
    assert (root / release.MANIFEST).read_bytes() == old
    updater.installed(root)
    assert_protected(root, protected)


def test_network_failure(releases, monkeypatch):
    root, _, _, protected = releases
    client = updater.ReleaseClient()
    monkeypatch.setattr(client, 'read', lambda *_: (_ for _ in ()).throw(OSError('network unavailable')))
    with pytest.raises(OSError):
        updater.update(root, client=client, environment=environment)
    assert updater.installed(root)['version'] == BASE_VERSION
    assert_protected(root, protected)


def test_environment_failure_restores_source(releases):
    root, _, second, protected = releases
    def failure(_):
        raise RuntimeError('synthetic dependency failure')
    with server(second) as client, pytest.raises(RuntimeError, match='源码已恢复'):
        updater.update(root, client=client, environment=failure)
    assert updater.installed(root)['version'] == BASE_VERSION
    journal = updater.journals(root)[0][2]
    assert journal['status'] == 'rolled_back'
    assert journal['phases']['code'] == 'passed'
    assert journal['phases']['environment'] == 'failed'
    assert_protected(root, protected)


def test_copy_failure_restores_source(releases, monkeypatch):
    root, _, second, protected = releases
    real_copy = shutil.copy2
    failed = False
    def failure(source, destination, *args, **kwargs):
        nonlocal failed
        if 'payload' in Path(source).parts and not failed:
            failed = True
            raise OSError('synthetic disk failure')
        return real_copy(source, destination, *args, **kwargs)
    monkeypatch.setattr(updater.shutil, 'copy2', failure)
    with server(second) as client, pytest.raises(RuntimeError, match='源码已恢复'):
        updater.update(root, client=client, environment=environment)
    assert failed
    assert updater.installed(root)['version'] == BASE_VERSION
    assert_protected(root, protected)


def test_rollback_refuses_later_user_edit(releases):
    root, _, second, protected = releases
    with server(second) as client:
        updater.update(root, client=client, environment=environment)
    (root / 'README.md').write_text('post upgrade user edit\n')
    with pytest.raises(ValueError, match='升级后源码已修改'):
        updater.rollback(root)
    assert (root / 'README.md').read_text() == 'post upgrade user edit\n'
    assert_protected(root, protected)


def test_git_worktree_refused_before_network(releases):
    root, _, _, _ = releases
    (root / '.git').write_text('gitdir: fixture')
    with pytest.raises(ValueError, match='Git 工作副本'):
        updater.update(root)


def test_new_file_collision_refused(releases):
    root, _, second, _ = releases
    path = root / 'docs/new_release_fixture.md'
    path.write_text('user content')
    with server(second) as client, pytest.raises(ValueError, match='冲突'):
        updater.update(root, client=client, environment=environment)
    assert path.read_text() == 'user content'


def test_downgrade_refused(releases):
    root, first, second, _ = releases
    with server(second) as client:
        updater.update(root, client=client, environment=environment)
    with server(first) as client, pytest.raises(ValueError, match='降级'):
        updater.update(root, tag='v' + BASE_VERSION, client=client, environment=environment)


@pytest.mark.parametrize('name', ['../escape.py', '/tmp/escape', 'data/raw/steal.csv',
                                 'config/study_config.yaml', 'docs\\file.md', 'docs/CON.md',
                                 'docs/file.md:stream', 'docs/./file.md', 'docs/file.md.'])
def test_unsafe_manifest_paths_refused(name):
    with pytest.raises(ValueError):
        release.managed_name(name)


@pytest.mark.parametrize('kind', ['traversal', 'symlink', 'case_collision', 'extra', 'changed_file', 'inner_manifest'])
def test_malicious_zip_refused(releases, tmp_path, kind):
    _, first, _, _ = releases
    archive = tmp_path / ('malicious-' + kind + '.zip')
    with ZipFile(first['archive']) as original, ZipFile(archive, 'w') as dest:
        for info in original.infolist():
            data = original.read(info)
            if kind == 'changed_file' and info.filename == 'tme_agent/README.md':
                data += b'bad'
            if kind == 'inner_manifest' and info.filename.endswith('/package_manifest.json'):
                data += b' '
            dest.writestr(info, data)
        if kind == 'traversal':
            dest.writestr('tme_agent/../escape.py', 'escape')
        elif kind == 'symlink':
            info = ZipInfo('tme_agent/docs/link.md'); info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            dest.writestr(info, '../README.md')
        elif kind == 'case_collision':
            dest.writestr('tme_agent/readme.md', 'collision')
        elif kind == 'extra':
            dest.writestr('tme_agent/data/raw/private.csv', 'unexpected')
    stage = tmp_path / ('stage-' + kind)
    with pytest.raises(ValueError):
        updater.unpack(archive, Path(first['manifest']), stage)
    assert not stage.exists()


@pytest.mark.skipif(__import__('os').name == 'nt', reason='Symlink creation requires Windows privilege')
def test_symlink_destination_refused(releases, tmp_path):
    root, _, second, _ = releases
    outside = tmp_path / 'outside'
    outside.mkdir()
    (root / '.runtime').symlink_to(outside, target_is_directory=True)
    with server(second) as client, pytest.raises(ValueError, match='符号链接'):
        updater.update(root, client=client, environment=environment)
    assert list(outside.iterdir()) == []


def test_initialize_preserves_existing_configuration(releases):
    root, _, _, _ = releases
    target = root / 'config/study_config.yaml'
    target.write_text('custom local configuration\n')
    assert initialize(root) == []
    assert target.read_text() == 'custom local configuration\n'


@pytest.mark.parametrize('url', ['http://github.com/x', 'https://example.org/x', 'https://github.com@evil.invalid/x'])
def test_non_github_transport_refused(url):
    with pytest.raises(ValueError):
        updater.permitted_url(url)


def test_version_validation():
    assert release.version('v8.0.1') == '8.0.1'
    for value in ['v8', '8.0.1-rc1', '../main', '08.0.0', 'v8.0.1\n']:
        with pytest.raises(ValueError):
            release.version(value)


def test_interrupted_update_requires_recovery(releases):
    root, _, second, protected = releases
    def interruption(_):
        raise KeyboardInterrupt('synthetic interruption')
    with server(second) as client, pytest.raises(KeyboardInterrupt):
        updater.update(root, client=client, environment=interruption)
    assert updater.installed(root)['version'] == NEXT_VERSION
    with server(second) as client, pytest.raises(ValueError, match='上次更新中断'):
        updater.update(root, client=client, environment=environment)
    assert updater.rollback(root)['version'] == BASE_VERSION
    assert_protected(root, protected)


def test_rollback_unchanged_source_edit_refused_before_any_write(releases):
    root, _, second, protected = releases
    with server(second) as client:
        updater.update(root, client=client, environment=environment)
    path = root / 'multiomics_tui/root.py'
    path.write_text('post update edit\n')
    before = (root / 'VERSION').read_bytes()
    with pytest.raises(ValueError, match='升级后源码已修改'):
        updater.rollback(root)
    assert (root / 'VERSION').read_bytes() == before
    assert path.read_text() == 'post update edit\n'
    assert_protected(root, protected)


def test_diagnostic_failure_has_separate_phase(releases):
    root, _, second, _ = releases
    def failure(_):
        raise updater.PreparationError('synthetic doctor failure', {'environment': 'passed', 'diagnostic': 'failed'})
    with server(second) as client, pytest.raises(RuntimeError, match='源码已恢复'):
        updater.update(root, client=client, environment=failure)
    journal = updater.journals(root)[0][2]
    assert journal['status'] == 'rolled_back'
    assert journal['phases'] == {'code': 'passed', 'environment': 'passed', 'diagnostic': 'failed'}

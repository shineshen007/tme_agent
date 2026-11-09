"""Verified public-source updates. No Git, shell, credentials or research-data writes."""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import uuid
import zipfile

from .public_release import (MANIFEST, SUMS, REPOSITORY, MAX_BYTES, file_sha, sha,
                             json_bytes, load_json, safe_name, safe_path, validate_manifest, version)

ACTIVE = {'starting', 'running', 'stopping', 'stalled', 'stop_failed'}
ROOT = Path(__file__).resolve().parents[1]


def save_json(path, value):
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    temp.write_bytes(json_bytes(value))
    temp.replace(path)


def now():
    return datetime.now(timezone.utc).isoformat()


def permitted_url(url, local_testing=False):
    parsed = urllib.parse.urlparse(url)
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError('Invalid update URL')
    if local_testing and parsed.scheme == 'http' and parsed.hostname in {'127.0.0.1', 'localhost'}:
        return url
    if parsed.scheme != 'https' or parsed.hostname not in {
        'api.github.com', 'github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com'
    } or parsed.port not in {None, 443}:
        raise ValueError('The update URL must use GitHub HTTPS')
    return url


class ReleaseClient:
    def __init__(self, api_base='https://api.github.com', *, local_testing=False):
        self.api_base = api_base.rstrip('/')
        self.local_testing = local_testing
        permitted_url(self.api_base, local_testing)

    def read(self, url, limit=MAX_BYTES):
        local_testing = self.local_testing

        class Redirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                permitted_url(newurl, local_testing)
                return super().redirect_request(req, fp, code, msg, headers, newurl)

        permitted_url(url, local_testing)
        request = urllib.request.Request(url, headers={'User-Agent': 'tme-agent-updater', 'Accept': 'application/vnd.github+json'})
        opener = urllib.request.build_opener(Redirect())
        with opener.open(request, timeout=30) as response:
            permitted_url(response.url, local_testing)
            data = response.read(limit + 1)
        if len(data) > limit:
            raise ValueError('Download exceeds the size limit')
        return data

    def release(self, tag=None):
        suffix = 'latest' if tag is None else 'tags/v' + version(tag)
        info = load_json(self.read(f'{self.api_base}/repos/{REPOSITORY}/releases/{suffix}', 2 * 1024 * 1024))
        if not isinstance(info, dict):
            raise ValueError('Invalid GitHub release response')
        v = version(info['tag_name'])
        if info.get('draft') or info.get('prerelease') or info['tag_name'] != 'v' + v:
            raise ValueError('Draft, prerelease or invalid versions are refused')
        if tag and version(tag) != v:
            raise ValueError('GitHub returned the wrong version')
        assets = {}
        for item in info['assets']:
            if item['name'] in assets:
                raise ValueError('Duplicate release asset')
            assets[item['name']] = item
        for name in (f'tme_agent-v{v}.zip', f'manifest-v{v}.json', f'SHA256SUMS-v{v}.txt'):
            if name not in assets:
                raise ValueError('Missing release asset: ' + name)
            permitted_url(assets[name]['browser_download_url'], self.local_testing)
        return v, assets

    def download(self, v, assets, directory):
        names = (f'tme_agent-v{v}.zip', f'manifest-v{v}.json')
        sums = self.read(assets[f'SHA256SUMS-v{v}.txt']['browser_download_url'], 16384).decode('utf-8')
        expected = {}
        for line in sums.splitlines():
            checksum, name = line.split('  ', 1)
            if name in expected or name not in names or len(checksum) != 64 or any(c not in '0123456789abcdef' for c in checksum):
                raise ValueError('Invalid asset checksum list')
            expected[name] = checksum
        if set(expected) != set(names):
            raise ValueError('Incomplete asset checksum list')
        for name in names:
            data = self.read(assets[name]['browser_download_url'], MAX_BYTES if name.endswith('.zip') else 2 * 1024 * 1024)
            if sha(data) != expected[name]:
                raise ValueError('Asset SHA256 mismatch: ' + name)
            api_digest = assets[name].get('digest')
            if api_digest and api_digest != 'sha256:' + sha(data):
                raise ValueError('Asset does not match the GitHub digest: ' + name)
            (directory / name).write_bytes(data)
        return directory / names[0], directory / names[1]


def unpack(archive, external_manifest, stage):
    """Validate every byte/path before creating the staged payload."""
    external = external_manifest.read_bytes()
    manifest = validate_manifest(load_json(external))
    expected = {item['path']: item for item in manifest['files']}
    with zipfile.ZipFile(archive) as z:
        entries = {}
        total = 0
        for entry in z.infolist():
            prefix, sep, name = entry.filename.partition('/')
            if prefix != 'tme_agent' or not sep or entry.is_dir():
                raise ValueError('Invalid release ZIP root')
            safe_name(name)
            if name in entries or name.casefold() in {s.casefold() for s in entries}:
                raise ValueError('Duplicate ZIP path')
            mode = entry.external_attr >> 16
            if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in {0, stat.S_IFREG}) or entry.flag_bits & 1:
                raise ValueError('ZIP contains links, special files or encrypted files')
            total += entry.file_size
            if total > MAX_BYTES + 4 * 1024 * 1024:
                raise ValueError('ZIP uncompressed size exceeds the limit')
            entries[name] = entry
        if set(entries) != set(expected) | {MANIFEST, SUMS}:
            raise ValueError('ZIP file set does not match the release manifest')
        if z.read(entries[MANIFEST]) != external:
            raise ValueError('Internal and external ZIP manifests differ')
        payload = {name: z.read(entry) for name, entry in entries.items()}
        for name, item in expected.items():
            data = payload[name]
            if sha(data) != item['sha256'] or len(data) != item['bytes']:
                raise ValueError('Release file verification failed: ' + name)
        wanted_sums = ''.join(f'{sha(payload[name])}  {name}\n' for name in sorted(set(expected) | {MANIFEST}))
        if payload[SUMS].decode('utf-8') != wanted_sums:
            raise ValueError('Internal release checksum list differs')
        if payload['VERSION'].decode().strip() != manifest['version']:
            raise ValueError('ZIP VERSION does not match the manifest')
    stage.mkdir()
    for name, data in payload.items():
        dest = safe_path(stage, name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        dest.chmod(expected[name]['mode'] if name in expected else 0o644)
    return manifest


def installed(root):
    manifest = validate_manifest(load_json(safe_path(root, MANIFEST).read_bytes()))
    for item in manifest['files']:
        path = safe_path(root, item['path'])
        if not path.is_file() or file_sha(path) != item['sha256'] or path.stat().st_size != item['bytes']:
            raise ValueError('Release file is missing or changed; nothing overwritten: ' + item['path'])
    if safe_path(root, 'VERSION').read_text().strip() != manifest['version']:
        raise ValueError('Local version does not match the manifest')
    # The manifest sorts with all other managed paths, rather than being appended.
    records = {item['path']: item['sha256'] for item in manifest['files']}
    records[MANIFEST] = file_sha(root / MANIFEST)
    sums = ''.join(f'{digest}  {name}\n' for name, digest in sorted(records.items()))
    if safe_path(root, SUMS).read_text(encoding='utf-8') != sums:
        raise ValueError('Local release checksum list was modified')
    return manifest


def refuse_git(root):
    if (root / '.git').exists() or (root / '.git').is_symlink():
        raise ValueError('This directory is a Git working copy; maintainers should use git pull --ff-only, then run bootstrap setup.')


@contextmanager
def maintenance(root):
    with ExitStack() as stack:
        for name in ('.runtime/public-update.lock', 'workspace/tui/admission.lock', 'workspace/tui/pipeline.lock'):
            path = safe_path(root, name)
            path.parent.mkdir(parents=True, exist_ok=True)
            stream = stack.enter_context(path.open('a+b'))
            try:
                if os.name == 'nt':
                    import msvcrt
                    if not path.stat().st_size:
                        stream.write(b'\0'); stream.flush()
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError('A task or another update process is active; complete or stop it first.') from exc
        for path in safe_path(root, 'workspace/tui/jobs').glob('*.json'):
            if path.is_symlink() or load_json(path.read_bytes()).get('status') in ACTIVE:
                raise RuntimeError('Active task records remain; complete or stop them first.')
        yield


def journals(root):
    base = safe_path(root, '.runtime/public-updates')
    rows = []
    for path in base.glob('*/journal.json'):
        safe_path(root, path.relative_to(root).as_posix())
        value = load_json(path.read_bytes())
        rows.append((value['created_at'], path.parent, value))
    return sorted(rows, key=lambda row: row[0], reverse=True)


def restore(root, backup):
    journal = load_json((backup / 'journal.json').read_bytes())
    if journal['root'] != str(root) or journal['status'] == 'preparing':
        raise ValueError('Backup belongs to another project or is not ready')
    if journal['status'] == 'rolled_back':
        return {'status': 'already_rolled_back'}
    # Unchanged managed files must also remain intact before any restoration.
    changed = {item['path'] for item in journal['changes']}
    previous = validate_manifest(load_json(safe_path(backup, 'original/' + MANIFEST).read_bytes()))
    for item in previous['files']:
        if item['path'] not in changed:
            target = safe_path(root, item['path'])
            if not target.is_file() or file_sha(target) != item['sha256']:
                raise ValueError('Source changed after upgrade; rollback overwrite refused: ' + item['path'])
    # Verify every backup and every current destination before restoring any file.
    for item in journal['changes']:
        name = item['path']
        if name not in {MANIFEST, SUMS}:
            from .public_release import managed_name
            managed_name(name)
        target = safe_path(root, name)
        current = file_sha(target) if target.is_file() else None
        if current not in {item['old_hash'], item['new_hash']}:
            raise ValueError('Source changed after upgrade; rollback overwrite refused: ' + name)
        if item['old_hash'] is not None:
            source = safe_path(backup, 'original/' + name)
            if file_sha(source) != item['old_hash']:
                raise ValueError('Rollback backup verification failed: ' + name)
    for item in reversed(journal['changes']):
        target = safe_path(root, item['path'])
        if item['old_hash'] is None:
            if target.exists():
                target.unlink()  # Only the recorded, hash-verified managed addition.
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_name(target.name + '.' + uuid.uuid4().hex + '.tmp')
            shutil.copy2(safe_path(backup, 'original/' + item['path']), temp)
            temp.replace(target)
    installed(root)
    journal['status'] = 'rolled_back'
    journal['rolled_back_at'] = now()
    save_json(backup / 'journal.json', journal)
    return {'status': 'rolled_back', 'version': journal['old_version'], 'backup': str(backup),
            'environment': 'existing dependency additions retained'}


def rollback(root):
    refuse_git(root)
    with maintenance(root):
        for _, backup, value in journals(root):
            if value['status'] not in {'rolled_back', 'preparing'}:
                return restore(root, backup)
        raise ValueError('No public release update is available to roll back')


class PreparationError(RuntimeError):
    def __init__(self, message, phases):
        super().__init__(message)
        self.phases = phases


def prepare_environment(root):
    py = root / ('.venv/Scripts/python.exe' if os.name == 'nt' else '.venv/bin/python')
    if not py.is_file():
        py = Path(sys.executable)
    result = subprocess.run([str(py), '-B', '-m', 'native_dist.bootstrap', 'setup', '--preserve-local-config', '--project-root', str(root)],
                            cwd=root, check=False)
    if result.returncode:
        report_path = safe_path(root, '.runtime/install_report.json')
        report = load_json(report_path.read_bytes()) if report_path.is_file() else {}
        diagnostic_failed = report.get('phase') == 'behavioral_selfcheck'
        raise PreparationError('Environment preparation or diagnostics failed; see .runtime/install_report.json and install.log',
                               {'environment': 'passed' if diagnostic_failed else 'failed',
                                'diagnostic': 'failed' if diagnostic_failed else 'pending'})
    return {'status': 'passed', 'diagnostic': '.runtime/doctor.json'}


def apply(root, stage, manifest, environment=prepare_environment):
    refuse_git(root)
    with maintenance(root):
        if any(value['status'] in {'applying', 'code_updated', 'recovery_failed'} for _, _, value in journals(root)):
            raise ValueError('The last update was interrupted; run update --rollback first')
        old = installed(root)
        old_version = tuple(map(int, old['version'].split('.')))
        new_version = tuple(map(int, manifest['version'].split('.')))
        if new_version < old_version:
            raise ValueError('Downgrade refused; use update --rollback')
        if new_version == old_version:
            if file_sha(root / MANIFEST) != file_sha(stage / MANIFEST):
                raise ValueError('Release content changed for the same version; overwrite refused')
            return {'status': 'already_updated', 'version': old['version']}
        old_items = {item['path']: item for item in old['files']}
        new_items = {item['path']: item for item in manifest['files']}
        for name in (MANIFEST, SUMS):
            old_items[name] = {'sha256': file_sha(root / name)}
            new_items[name] = {'sha256': file_sha(stage / name)}
        changes = []
        for name in sorted(set(old_items) | set(new_items)):
            target = safe_path(root, name)
            if name not in old_items and target.exists():
                raise ValueError('New release file conflicts with a local file: ' + name)
            previous = old_items.get(name, {}).get('sha256')
            following = new_items.get(name, {}).get('sha256')
            if previous != following:
                changes.append({'path': name, 'old_hash': previous, 'new_hash': following})
        base = safe_path(root, '.runtime/public-updates')
        base.mkdir(parents=True, exist_ok=True)
        backup = base / uuid.uuid4().hex
        backup.mkdir()
        journal = {'schema_version': 1, 'root': str(root), 'created_at': now(), 'status': 'preparing',
                   'old_version': old['version'], 'new_version': manifest['version'], 'changes': changes,
                   'phases': {'code': 'pending', 'environment': 'pending', 'diagnostic': 'pending'}}
        save_json(backup / 'journal.json', journal)
        for item in changes:
            if item['old_hash'] is not None:
                dest = safe_path(backup, 'original/' + item['path'])
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(safe_path(root, item['path']), dest)
                if file_sha(dest) != item['old_hash']:
                    raise ValueError('Source backup verification failed')
        journal['status'] = 'applying'
        save_json(backup / 'journal.json', journal)
        try:
            for item in changes:
                target = safe_path(root, item['path'])
                if item['new_hash'] is None:
                    target.unlink()
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    temp = target.with_name(target.name + '.' + uuid.uuid4().hex + '.tmp')
                    shutil.copy2(safe_path(stage, item['path']), temp)
                    temp.replace(target)
            installed(root)
            journal['status'] = 'code_updated'
            journal['phases']['code'] = 'passed'
            save_json(backup / 'journal.json', journal)
            environment(root)
            journal['phases'].update(environment='passed', diagnostic='passed')
            journal['status'] = 'complete'
            save_json(backup / 'journal.json', journal)
        except Exception as exc:
            journal['error_type'] = type(exc).__name__
            if isinstance(exc, PreparationError):
                journal['phases'].update(exc.phases)
            else:
                journal['phases']['environment' if journal['phases']['code'] == 'passed' else 'code'] = 'failed'
            save_json(backup / 'journal.json', journal)
            try:
                restore(root, backup)
            except Exception as recovery:
                journal['status'] = 'recovery_failed'
                journal['recovery_error_type'] = type(recovery).__name__
                save_json(backup / 'journal.json', journal)
                raise RuntimeError('Update and automatic recovery failed; backup retained: ' + str(backup) + '; ' + str(recovery)) from exc
            raise RuntimeError('Update failed; release source was restored. Installed dependencies were retained. ' + str(exc)) from exc
        return {'status': 'updated', 'version': manifest['version'], 'backup': str(backup),
                'phases': journal['phases'], 'next_step': 'Close and restart the interface'}


def update(root, *, check=False, tag=None, client=None, environment=prepare_environment):
    root = Path(root).absolute()
    if not check:
        refuse_git(root)
    current = installed(root)
    client = client or ReleaseClient()
    v, assets = client.release(tag)
    newer = tuple(map(int, v.split('.'))) > tuple(map(int, current['version'].split('.')))
    if check:
        return {'status': 'update_available' if newer else 'up_to_date', 'installed': current['version'], 'available': v}
    # Protected storage is checked before tempfile creates anything.
    downloads = safe_path(root, '.runtime/public-downloads')
    downloads.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='release-', dir=downloads) as folder:
        folder = Path(folder)
        archive, external = client.download(v, assets, folder)
        stage = folder / 'payload'
        manifest = unpack(archive, external, stage)
        if manifest['version'] != v:
            raise ValueError('Asset version does not match the GitHub tag')
        return apply(root, stage, manifest, environment)


def main(argv=None, root=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', type=Path, default=root or ROOT)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument('--rollback', action='store_true')
    selection.add_argument('--check', action='store_true')
    parser.add_argument('--version', type=version, dest='tag')
    args = parser.parse_args(argv)
    if args.rollback and args.tag:
        parser.error('--rollback and --version cannot be used together')
    try:
        target = args.project_root.absolute()
        result = rollback(target) if args.rollback else update(target, check=args.check, tag=args.tag)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, zipfile.BadZipFile) as exc:
        print(f'Update failed: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

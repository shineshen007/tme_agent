"""Build the data-free public release from an explicit source allowlist (stdlib)."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import zipfile

REPOSITORY = 'shineshen007/tme_agent'
ROOT = Path(__file__).resolve().parents[1]
MANIFEST = 'package_manifest.json'
SUMS = 'PACKAGE_SHA256SUMS.txt'
TOP = {'LICENSE', 'README.md', 'VERSION', 'PUBLIC_FILES.json', '.gitignore',
       'CURRENT_STATE.md', 'CHANGELOG.md', 'PROJECT_CONTEXT.md', 'requirements.txt',
       'setup_windows.cmd', 'setup_macos.command', 'start_windows.cmd',
       'start_macos.command', 'diagnose_windows.cmd', 'diagnose_macos.command'}
AREAS = {'multiomics_tui': {'.py', '.tcss', '.md', '.R'}, 'orchestrator': {'.py'},
         'native_dist': {'.py', '.txt', '.json', '.c', '.m', '.ps1', '.iss'},
         'scripts': {'.py', '.R', '.sh'}, 'schemas': {'.json'}, 'prompts': {'.md'},
         'tests': {'.py'}, 'docs': {'.md', '.json'}, 'examples': {'.csv', '.md'},
         '.github': {'.yml', '.yaml'}}
GENERATED = {MANIFEST, SUMS}
MAX_BYTES = 100 * 1024 * 1024
# These are credentials, not generic test strings such as TEST_API_KEY.
SECRET = re.compile(rb'\b(?:sk-[A-Za-z0-9_-]{24,}|gh[pousr]_[A-Za-z0-9]{30,})\b|-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----')


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def version(value: str) -> str:
    value = value.removeprefix('v')
    if not re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', value):
        raise ValueError('版本必须是 X.Y.Z 或 vX.Y.Z')
    return value


def safe_name(name: str) -> str:
    if not isinstance(name, str) or not name or '\\' in name or '\x00' in name:
        raise ValueError('不安全的路径')
    parts = name.split('/')
    reserved = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}
    if PurePosixPath(name).is_absolute() or any(
        p in {'', '.', '..'} or ':' in p or p.endswith((' ', '.')) or
        p.split('.')[0].upper() in reserved or any(ord(c) < 32 for c in p)
        for p in parts
    ):
        raise ValueError('不安全的路径：' + name)
    return name


def safe_path(root: Path, name: str) -> Path:
    safe_name(name)
    path = root
    for part in name.split('/'):
        path = path / part
        if path.is_symlink():
            raise ValueError('拒绝符号链接：' + name)
    return path


def managed_name(name: str) -> str:
    safe_name(name)
    if name in TOP:
        return name
    p = PurePosixPath(name)
    if name == 'config/study_config.example.yaml':
        return name
    if p.parts[0] in AREAS and p.suffix in AREAS[p.parts[0]] and not any(
        s in {'__pycache__', '.git', '.venv', '.runtime', '.cache'} for s in p.parts
    ):
        return name
    raise ValueError('发行禁止管理此路径：' + name)


def json_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')


def load_json(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('JSON 重复字段：' + key)
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=unique)


def validate_manifest(manifest):
    if not isinstance(manifest, dict):
        raise ValueError('发行清单必须是 JSON 对象')
    if manifest.get('schema_version') != 1 or manifest.get('delivery') != 'public_source' or manifest.get('repository') != REPOSITORY:
        raise ValueError('非本仓库的公开发行清单')
    v = version(manifest['version'])
    if v != manifest['version'] or manifest.get('tag') != 'v' + v:
        raise ValueError('发行版本不一致')
    files = manifest.get('files')
    if not isinstance(files, list) or not 1 <= len(files) <= 3000:
        raise ValueError('发行清单文件数异常')
    seen = set()
    total = 0
    for item in files:
        name = managed_name(item['path'])
        if name.casefold() in seen:
            raise ValueError('重复或大小写冲突的发行路径：' + name)
        seen.add(name.casefold())
        if not re.fullmatch(r'[0-9a-f]{64}', item['sha256']):
            raise ValueError('SHA256 格式错误：' + name)
        if type(item['bytes']) is not int or not 0 <= item['bytes'] <= MAX_BYTES or item.get('mode') not in {0o644, 0o755}:
            raise ValueError('发行文件元数据错误：' + name)
        total += item['bytes']
    if total > MAX_BYTES or not {'version', 'license', 'public_files.json', 'multiomics_tui/__main__.py', 'native_dist/public_update.py', 'config/study_config.example.yaml'} <= seen:
        raise ValueError('发行不完整或体积超限')
    if manifest.get('source_data') != []:
        raise ValueError('公开发行不允许携带研究数据清单')
    return manifest


def source_files(root: Path):
    declaration = safe_path(root, 'PUBLIC_FILES.json')
    names = load_json(declaration.read_bytes())['files']
    if len(names) != len({n.casefold() for n in names}):
        raise ValueError('公开白名单重复')
    for name in sorted(names):
        managed_name(name)
        path = safe_path(root, name)
        if not path.is_file():
            raise ValueError('公开文件缺失：' + name)
        data = path.read_bytes()
        if SECRET.search(data):
            raise ValueError('公开文件疑似包含凭据：' + name)
        # Local source paths and private-study evidence must not enter public files.
        if (b'/mnt/' + b'8T_Disk2/') in data or (b'/home/' + b'shenxia/') in data:
            raise ValueError('公开文件包含本机绝对路径：' + name)
        yield name, path, data


def build(root: Path, output: Path, expected_version=None):
    v = version((root / 'VERSION').read_text(encoding='utf-8').strip())
    if expected_version and version(expected_version) != v:
        raise ValueError('标签与 VERSION 不一致')
    records = []
    members = {}
    for name, path, data in source_files(root):
        mode = 0o755 if path.suffix in {'.command', '.sh'} else 0o644
        members[name] = (data, mode)
        records.append({'path': name, 'bytes': len(data), 'sha256': sha(data), 'mode': mode})
    if members['VERSION'][0].decode().strip() != v:
        raise ValueError('VERSION 不在公开白名单内')
    manifest = {'schema_version': 1, 'delivery': 'public_source', 'repository': REPOSITORY,
                'version': v, 'tag': 'v' + v, 'license': 'GPL-3.0-or-later',
                'source_data': [], 'files': records,
                'validation_status': {'windows': 'native_pending', 'macos_intel': 'native_pending',
                                      'macos_arm64': 'native_pending', 'real_api': 'not_tested'}}
    validate_manifest(manifest)
    members[MANIFEST] = (json_bytes(manifest), 0o644)
    sums = ''.join(f'{sha(data)}  {name}\n' for name, (data, _) in sorted(members.items()))
    members[SUMS] = (sums.encode(), 0o644)
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f'tme_agent-v{v}.zip'
    external = output / f'manifest-v{v}.json'
    checksums = output / f'SHA256SUMS-v{v}.txt'
    for path in (archive, external, checksums):
        if path.exists():
            raise FileExistsError('不覆盖既有发行：' + str(path))
    with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for name, (data, mode) in sorted(members.items()):
            entry = zipfile.ZipInfo('tme_agent/' + name, (2026, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = (stat.S_IFREG | mode) << 16
            z.writestr(entry, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    external.write_bytes(members[MANIFEST][0])
    checksums.write_text(f'{file_sha(archive)}  {archive.name}\n{file_sha(external)}  {external.name}\n', encoding='utf-8')
    return {'version': v, 'files': len(records), 'bytes': archive.stat().st_size,
            'archive': str(archive), 'sha256': file_sha(archive), 'manifest': str(external), 'checksums': str(checksums)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    prepare = sub.add_parser('prepare', help='Validate sources, then change VERSION before committing')
    prepare.add_argument('--version', required=True)
    release = sub.add_parser('build', help='Build ZIP, manifest and checksum list without private data')
    release.add_argument('--output', type=Path, default=ROOT / 'dist')
    release.add_argument('--expected-version')
    sub.add_parser('audit', help='Validate every declared public file')
    args = parser.parse_args(argv)
    try:
        if args.action == 'build':
            result = build(ROOT, args.output, args.expected_version)
        else:
            rows = list(source_files(ROOT))
            if args.action == 'prepare':
                v = version(args.version)
                old = version((ROOT / 'VERSION').read_text().strip())
                if tuple(map(int, v.split('.'))) < tuple(map(int, old.split('.'))):
                    raise ValueError('准备新版本不能降低版本号')
                (ROOT / 'VERSION').write_text(v + '\n', encoding='utf-8')
            result = {'status': 'passed', 'files': len(rows), 'version': (ROOT / 'VERSION').read_text().strip()}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f'发行失败：{exc}', file=__import__('sys').stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

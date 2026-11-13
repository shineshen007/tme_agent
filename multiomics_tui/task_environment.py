"""Trusted task-local dependency preparation. Never called by generated scripts.

Managers resolve/download first; package code is installed with network denied.
A receipt is published only after the installed APIs pass the existing probe.
"""
from __future__ import annotations

import copy
import hashlib
import gzip
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import tarfile
import tomllib
import urllib.request
import uuid
from pathlib import Path

from .environment_contract import PROTOCOL, approved_spec, validate_spec
from .task_sessions import canonical_hash

R_BASE = {'base', 'compiler', 'datasets', 'grDevices', 'graphics', 'grid', 'methods',
          'parallel', 'splines', 'stats', 'stats4', 'tcltk', 'tools', 'utils'}


class EnvironmentError(RuntimeError):
    pass


def specification(bundle, bindings):
    selected = {b['id'] for b in bindings}
    dependencies, interpreters = {}, {}
    for row in bundle['methods']:
        c, e, source = row['candidate'], row['environment'], row['software']
        if c['id'] not in selected:
            continue
        language, package = c['language'], c['package']
        interpreters[language] = e['runtime']
        if package == 'stdlib' or language == 'r' and package in R_BASE:
            continue
        origin = source.get('install_source') or {'kind': 'pypi' if language == 'python' else
                   'bioconductor' if 'bioconductor.org' in source['registry_url'] else 'cran'}
        key = (language, package)
        dep = {'language': language, 'package': package, 'version': source['version'],
               'source': origin, 'apis': list(c['apis'])}
        if key in dependencies:
            old = dependencies[key]
            if (old['version'], old['source']) != (dep['version'], dep['source']):
                raise EnvironmentError('Conflicting versions/sources for ' + package)
            old['apis'] = sorted(set(old['apis'] + dep['apis']))
        else:
            dependencies[key] = dep
    spec = {'protocol': PROTOCOL, 'interpreters': interpreters,
            'dependencies': list(dependencies.values()),
            'policy': {'system_install': False, 'required_dependencies_only': True}}
    validate_spec(spec)
    return spec


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write_json(path, data):
    path = Path(path)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with tmp.open('x', encoding='utf-8') as stream:
        json.dump(data, stream, ensure_ascii=False, sort_keys=True, indent=2)
    os.replace(tmp, path)


def inventory(folder):
    result = {}
    for p in sorted(Path(folder).rglob('*')):
        # Linux venv creates lib64 -> lib. Bind internal directory aliases without
        # following them recursively; reject links escaping the inventoried tree.
        if p.is_symlink() and p.is_dir():
            target = p.resolve(strict=True)
            if not target.is_relative_to(Path(folder).resolve()):
                raise EnvironmentError('Linked environment directory refused: ' + str(p))
            result[p.relative_to(folder).as_posix()] = canonical_hash({'symlink':os.readlink(p), 'target':target.relative_to(Path(folder).resolve()).as_posix()})
            continue
        if p.is_file():
            result[p.relative_to(folder).as_posix()] = digest(p)
    return result


def bound_config(cfg, receipt):
    result = copy.deepcopy(cfg)
    runtime = result.setdefault('runtime', {})
    runtime['task_environment'] = receipt['directory']
    runtime['task_verified_apis'] = [{k: row[k] for k in ('id', 'package', 'version', 'apis')} for row in receipt.get('apis', [])]
    if receipt.get('python'):
        runtime['task_python'] = receipt['python']
    if receipt.get('r_library'):
        runtime['task_r_library'] = receipt['r_library']
    return result


def verify_receipt(root, plan, receipt):
    from .planning_evidence import _path
    folder = _path(Path(root), receipt['directory'])
    if not receipt['directory'].startswith('.runtime/task_environments/'):
        raise EnvironmentError('Environment is outside task environment storage')
    if receipt.get('spec') != plan['environment_spec'] or receipt['spec_hash'] != canonical_hash(plan['environment_spec']):
        raise EnvironmentError('Environment specification changed')
    if receipt['platform'] != platform.platform():
        raise EnvironmentError('Environment platform changed')
    expected_python = folder / 'runtime/python' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if receipt.get('python') and (Path(receipt['python']) != expected_python or receipt['python'] not in receipt['interpreters']):
        raise EnvironmentError('Environment Python binding changed')
    if receipt.get('r_library') and Path(receipt['r_library']) != folder / 'runtime/r-library':
        raise EnvironmentError('Environment R library binding changed')
    languages = {d['language'] for d in plan['environment_spec']['dependencies']}
    if ('python' in languages) != bool(receipt.get('python')) or ('r' in languages) != bool(receipt.get('r_library')):
        raise EnvironmentError('Environment runtime binding is incomplete')
    if receipt['files'] != inventory(folder / 'runtime'):
        raise EnvironmentError('Installed environment changed')
    for relative, expected in receipt['locks'].items():
        if digest(_path(folder, relative)) != expected:
            raise EnvironmentError('Dependency lock changed')
    for executable, expected in receipt['interpreters'].items():
        if digest(executable) != expected:
            raise EnvironmentError('Interpreter changed')
    return receipt


class Installer:
    """Trusted manager adapter. Package install hooks only run under the guard."""
    def __init__(self, root, folder, cfg, progress):
        self.root, self.folder, self.cfg, self.progress = Path(root), Path(folder), cfg, progress
        if sys.platform == 'linux' and not shutil.which('bwrap'):
            raise EnvironmentError('Task installation requires an existing bubblewrap executable on Linux')
        self.runtime = self.folder / 'runtime'
        self.runtime.mkdir(exist_ok=True)
        from .task_runner import _step_environment
        self.env = _step_environment(root, folder, cfg, [])
        self.env.update(UV_CACHE_DIR=str(folder / 'uv-cache'), UV_PYTHON_DOWNLOADS='never',
                        UV_NO_CONFIG='1', UV_LINK_MODE='copy', PIP_CONFIG_FILE=os.devnull,
                        PIP_DISABLE_PIP_VERSION_CHECK='1', PYTHONNOUSERSITE='1',
                        PKG_SYSREQS='false', PKG_PACKAGE_CACHE_DIR=str(folder / 'pak-cache'),
                        PKG_METADATA_CACHE_DIR=str(folder / 'pak-metadata'),
                        PKG_CRAN_MIRROR='https://cloud.r-project.org', PKG_GIT_SUBMODULES='false')
        # Do not inherit site/user pip, uv, git, R or credential configuration.
        self.env['R_ENVIRON_USER'] = os.devnull
        self.env['R_PROFILE_USER'] = os.devnull
        self.env.pop('R_LIBS', None)

    def run(self, argv, stage, guarded=False):
        self.progress(stage)
        argv = list(map(str, argv))
        env = dict(self.env)
        protected_tools = {name: inventory(self.folder / name) for name in ('tools', 'r-tools') if (self.folder / name).is_dir()} if guarded and stage != 'installing_tools' else {}
        frozen_locks = {p.name: digest(p) for p in self.folder.glob('*.lock') if p.name != 'install.lock'}
        if guarded:
            if sys.platform == 'darwin':
                from .macos_guard import snapshot_runtimes
                env['TASK_RUNTIME_READS'] = json.dumps(json.loads(env['TASK_RUNTIME_READS']) + [str(self.folder)])
                env['TASK_RUNTIME_PATHS'] = json.dumps(json.loads(env['TASK_RUNTIME_PATHS']) + snapshot_runtimes([str(self.folder), argv[0]]))
            env['TASK_INSTALLER'] = '1'
            argv = [sys.executable, str(Path(__file__).with_name('task_guard.py')),
                    str(self.folder), '--', *argv]
        with (self.folder / 'install.log').open('ab') as log:
            log.write(('\n' + stage + ': ' + json.dumps(argv) + '\n').encode())
            log.flush()
            result = subprocess.run(argv, cwd=self.folder, env=env, stdout=log,
                                    stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    timeout=3600, check=False)
        if any(inventory(self.folder / name) != before for name, before in protected_tools.items()):
            raise EnvironmentError('Package installation changed trusted package manager files')
        if frozen_locks != {name: digest(self.folder / name) for name in frozen_locks}:
            raise EnvironmentError('Dependency lock changed during installation')
        if result.returncode:
            raise EnvironmentError(f'{stage} failed ({result.returncode}); see {self.folder / "install.log"}')

    def download_artifact(self, url, target, *, expected_sha256=None, expected_size=None):
        """Resume immutable public artifacts with curl when available; never run them."""
        from .planning_evidence import _url
        from urllib.parse import urlsplit
        from orchestrator.llm_client import _NoRedirect
        host = urlsplit(url).hostname
        if host not in {'files.pythonhosted.org', 'cloud.r-project.org', 'codeload.github.com'}:
            raise EnvironmentError('Artifact source is outside approved public package hosts')
        _url(url, {host})
        target = Path(target)
        partial = target.with_name(target.name + '.partial')
        if not target.exists():
            curl = shutil.which('curl')
            if curl:
                for attempt in range(3):
                    try:
                        self.run([curl, '--fail', '--proto', '=https', '--connect-timeout', '30',
                                  '--max-time', '600', '--continue-at', '-', '--output', partial, url],
                                 'downloading_artifact:' + str(attempt + 1))
                        break
                    except EnvironmentError:
                        if attempt == 2:
                            raise
            else:
                opener = urllib.request.build_opener(_NoRedirect)
                with opener.open(url, timeout=120) as response, partial.open('wb') as output:
                    total = 0
                    while chunk := response.read(65536):
                        total += len(chunk)
                        if total > 256 * 1024 * 1024:
                            raise EnvironmentError('Artifact download exceeds size limit')
                        output.write(chunk)
            if expected_size is not None and partial.stat().st_size != expected_size:
                raise EnvironmentError('Artifact download is incomplete')
            if expected_sha256 is not None and digest(partial) != expected_sha256:
                raise EnvironmentError('Artifact download checksum mismatch')
            partial.replace(target)
        if expected_sha256 is not None and digest(target) != expected_sha256:
            raise EnvironmentError('Cached artifact checksum mismatch')
        return digest(target)

    def bootstrap_uv(self, python, tools):
        from .planning_evidence import _url
        from orchestrator.llm_client import _NoRedirect
        from packaging.tags import sys_tags
        from packaging.utils import parse_wheel_filename
        version = '0.13.7'
        url = 'https://pypi.org/pypi/uv/' + version + '/json'
        _url(url, {'pypi.org'})
        opener = urllib.request.build_opener(_NoRedirect)
        self.progress('downloading_tools')
        request = urllib.request.Request(url, headers={'Accept-Encoding': 'gzip', 'User-Agent': 'multiomics-task-environment'})
        with opener.open(request, timeout=60) as response:
            content = response.read(1024 * 1024)
            if response.headers.get('Content-Encoding') == 'gzip':
                content = gzip.decompress(content)
            metadata = json.loads(content)
        if metadata['info']['name'] != 'uv' or metadata['info']['version'] != version:
            raise EnvironmentError('uv bootstrap registry identity mismatch')
        compatible = set(sys_tags())
        matches = [item for item in metadata['urls'] if item['filename'].endswith('.whl')
                   and parse_wheel_filename(item['filename'])[3] & compatible and not item.get('yanked')]
        if not matches:
            raise EnvironmentError('No compatible official uv wheel for this interpreter/platform')
        item = sorted(matches, key=lambda row: row['filename'])[0]
        _url(item['url'], {'files.pythonhosted.org'})
        archive = tools / item['filename']
        self.download_artifact(item['url'], archive, expected_sha256=item['digests']['sha256'], expected_size=item['size'])
        write_json(self.folder / 'uv.bootstrap.json', {'version': version, 'url': item['url'], 'sha256': digest(archive)})
        self.run([python, '-m', 'pip', '--isolated', 'install', '--no-index', '--no-deps',
                  '--only-binary=:all:', archive], 'installing_tools')

    def source_tree(self, dependency):
        from .planning_evidence import _url
        origin = dependency['source']
        slug = origin['url'].removeprefix('https://github.com/')
        if '/' not in slug or origin['url'] != 'https://github.com/' + slug:
            raise EnvironmentError('Unsupported author archive source')
        url = 'https://codeload.github.com/' + slug + '/tar.gz/' + origin['commit']
        _url(url, {'codeload.github.com'})
        directory = self.folder / 'sources' / dependency['package']
        directory.mkdir(parents=True, exist_ok=True)
        archive = directory / 'source.tar.gz'
        record = directory / 'source.json'
        if not archive.exists():
            checksum = self.download_artifact(url, archive)
            write_json(record, {'url': url, 'sha256': checksum, 'commit': origin['commit']})
        recorded = json.loads(record.read_text())
        if recorded['url'] != url or recorded['sha256'] != digest(archive):
            raise EnvironmentError('Author archive changed')
        extracted = directory / ('tree_' + uuid.uuid4().hex)
        extracted.mkdir()
        with tarfile.open(archive) as stream:
            members = stream.getmembers()
            if sum(m.size for m in members) > 1024 * 1024 * 1024 or len(members) > 100000:
                raise EnvironmentError('Author archive expanded size limit exceeded')
            if any(not (m.isfile() or m.isdir()) for m in members):
                raise EnvironmentError('Links and special files in author archives are refused')
            stream.extractall(extracted, filter='data')
        children = list(extracted.iterdir())
        if len(children) != 1 or not children[0].is_dir():
            raise EnvironmentError('Author archive root is ambiguous')
        return children[0]

    def python(self, dependencies):
        tools = self.folder / 'tools'
        python = tools / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        if not python.exists():
            self.run([sys.executable, '-m', 'venv', '--copies', tools], 'preparing_tools')
        uv = tools / ('Scripts/uv.exe' if os.name == 'nt' else 'bin/uv')
        if not uv.exists():
            existing = shutil.which('uv')
            if existing:
                checked = subprocess.run([existing, '--version'], env=self.env, capture_output=True,
                                         text=True, check=True, timeout=15)
                from packaging.version import Version
                version = checked.stdout.split()[1]
                if not Version('0.9') <= Version(version) < Version('0.14'):
                    existing = None
            if existing:
                uv.parent.mkdir(exist_ok=True, parents=True)
                shutil.copyfile(Path(existing).resolve(), uv)
                uv.chmod(0o700)
                write_json(self.folder / 'uv.bootstrap.json',
                           {'version': version, 'source': 'existing_local_tool',
                            'source_path': str(Path(existing).resolve()), 'sha256': digest(uv)})
            else:
                self.bootstrap_uv(python, tools)
        self.run([uv, '--version'], 'manager_version')
        requirements = self.folder / 'requirements.in'
        entries, source_trees = [], []
        for d in dependencies:
            if d['source']['kind'] == 'author_repository':
                source = self.source_tree(d)
                metadata = tomllib.loads((source / 'pyproject.toml').read_text())
                project = metadata.get('project', {})
                from packaging.utils import canonicalize_name
                if canonicalize_name(project.get('name', '')) != canonicalize_name(d['package']) or project.get('version') != d['version']:
                    raise EnvironmentError('Author manifest differs from approved package')
                if 'dependencies' in project.get('dynamic', []):
                    raise EnvironmentError('Dynamic source dependencies need an explicit build recipe')
                source_requirements = project.get('dependencies', []) + metadata.get('build-system', {}).get('requires', ['setuptools', 'wheel'])
                from packaging.requirements import Requirement
                for text in source_requirements:
                    requirement = Requirement(text)
                    if requirement.url:
                        raise EnvironmentError('Author dependency introduces an unapproved URL')
                    entries.append(str(requirement))
                source_trees.append(source)
            else:
                entries.append(d['package'] + '==' + d['version'])
        # Probe helper dependency, not an analytical method.
        entries.append('packaging')
        requirements.write_text('\n'.join(entries) + '\n', encoding='utf-8')
        lock = self.folder / 'requirements.lock'
        if not lock.exists():
            self.run([uv, 'pip', 'compile', '--no-config', '--no-build', '--generate-hashes',
                      '--index-url', 'https://pypi.org/simple', '--python', sys.executable,
                      requirements, '-o', lock], 'resolving_dependencies')
        from packaging.requirements import Requirement
        for line in lock.read_text().replace('\\\n', ' ').splitlines():
            line = line.split('#', 1)[0].split('--hash=', 1)[0].strip()
            if not line:
                continue
            item = Requirement(line)
            if item.url or not str(item.specifier).startswith('=='):
                raise EnvironmentError('Resolved dependency introduces an unapproved source or unpinned version')
        wheels = self.folder / 'wheels'
        wheels.mkdir(exist_ok=True)
        # Wheels have static metadata; downloading them never invokes a build backend.
        self.run([python, '-m', 'pip', '--isolated', 'download', '--only-binary=:all:',
                  '--require-hashes', '--index-url', 'https://pypi.org/simple', '--timeout', '60', '--retries', '2',
                  '-r', lock, '-d', wheels], 'downloading_packages')
        write_json(self.folder / 'python.artifacts.json', inventory(wheels))
        target = self.runtime / 'python'
        exe = target / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        if not exe.exists():
            base_python = (Path(self.env['MULTIOMICS_RUNTIME_ROOT']) / '.runtime/sandbox_runtime/python/python.exe' if os.name == 'nt' else Path(sys.executable))
            self.run([base_python, '-m', 'venv', '--copies', '--without-pip', target], 'preparing_environment')
        self.run([uv, 'pip', 'sync', '--offline', '--no-config', '--no-build', '--require-hashes',
                  '--no-index', '--find-links', wheels, '--python', exe, lock], 'installing_packages', True)
        for source in source_trees:
            built = self.folder / ('built_' + uuid.uuid4().hex)
            built.mkdir()
            self.run([uv, 'build', '--offline', '--no-config', '--no-build-isolation', '--wheel',
                      '--python', exe, '--out-dir', built, source], 'building_author_package', True)
            artifacts = list(built.glob('*.whl'))
            if len(artifacts) != 1:
                raise EnvironmentError('Author build must produce exactly one wheel')
            self.run([uv, 'pip', 'install', '--offline', '--no-config', '--no-deps', '--no-index',
                      '--python', exe, artifacts[0]], 'installing_author_package', True)
        self.run([uv, 'pip', 'check', '--offline', '--python', exe], 'verifying_dependencies', True)
        if json.loads((self.folder / 'python.artifacts.json').read_text()) != inventory(wheels):
            raise EnvironmentError('Downloaded Python artifacts changed during installation')
        return str(exe), {'requirements.lock': digest(lock)}

    def r(self, dependencies):
        from .actions import rscript_path
        rscript = shutil.which(rscript_path(self.root)) or rscript_path(self.root)
        library = self.runtime / 'r-library'
        library.mkdir(exist_ok=True)
        tools = self.folder / 'r-tools'
        tools.mkdir(exist_ok=True)
        self.env['R_LIBS_USER'] = os.pathsep.join([str(library), str(tools)])
        self.env['R_LIBS_SITE'] = str(library)
        q = json.dumps
        # Bootstrap pak from a downloaded official source, under the same offline guard.
        download = self.folder / 'download_pak.R'
        download.write_text('options(timeout=180)\n'
            'kind <- .Platform$pkgType\n'
            'extension <- if(kind == "source") ".tar.gz" else if(.Platform$OS.type == "windows") ".zip" else ".tgz"\n'
            'url <- paste0(utils::contrib.url("https://cloud.r-project.org",type=kind),"/pak_0.11.2",extension)\n'
            'writeLines(kind,' + q(str(tools / 'package_type.txt')) + ')\n'
            'writeLines(url,' + q(str(tools / 'source_url.txt')) + ')\n'
            'target <- file.path(' + q(str(tools)) + ',paste0("pak_0.11.2",extension))\n'
            'writeLines(target,' + q(str(tools / 'target_path.txt')) + ')\n', encoding='utf-8')
        if not (tools / 'pak').is_dir():
            self.run([rscript, '--vanilla', download], 'resolving_tools')
            self.download_artifact((tools / 'source_url.txt').read_text().strip(), Path((tools / 'target_path.txt').read_text().strip()))
            archives = [p for p in tools.glob('pak_*') if p.name.endswith(('.tar.gz', '.tgz', '.zip'))]
            if len(archives) != 1:
                raise EnvironmentError('pak bootstrap archive is ambiguous')
            write_json(self.folder / 'pak.bootstrap.json', {'version': '0.11.2',
                'url': (tools / 'source_url.txt').read_text().strip(), 'sha256': digest(archives[0])})
            bootstrap = self.folder / 'install_pak.R'
            bootstrap.write_text('utils::install.packages(' + q(str(archives[0])) + ', repos=NULL, type=readLines(' + q(str(tools / 'package_type.txt')) + '), lib=' + q(str(tools)) + ')\n'
                                 'stopifnot(requireNamespace("pak",quietly=TRUE), as.character(utils::packageVersion("pak"))=="0.11.2")\n', encoding='utf-8')
            self.run([rscript, '--vanilla', bootstrap], 'installing_tools', True)
        refs = []
        for d in dependencies:
            if d['source']['kind'] == 'author_repository':
                source = self.source_tree(d)
                from email.parser import Parser
                metadata = dict(Parser().parsestr((source / 'DESCRIPTION').read_text()))
                if metadata.get('Package') != d['package'] or metadata.get('Version') != d['version']:
                    raise EnvironmentError('Author R manifest differs from approved package')
                if metadata.get('Remotes'):
                    raise EnvironmentError('Author R dependency introduces unapproved remote sources')
                refs.append('local::' + str(source))
            else:
                refs.append(d['package'] + '@' + d['version'])
        refs += ['jsonlite', 'digest']
        vector = 'c(' + ','.join(q(v) for v in refs) + ')'
        lock = self.folder / 'r-packages.lock'
        prepare = self.folder / 'prepare_packages.R'
        prepare.write_text('options(repos=c(CRAN="https://cloud.r-project.org"), Ncpus=2, timeout=1800, pkg.sysreqs=FALSE)\n'
            + ('pak::lockfile_create(' + vector + ', lockfile=' + q(str(lock)) + ', lib=' + q(str(library)) + ', dependencies=NA)\n' if not lock.exists() else ''), encoding='utf-8')
        self.run([rscript, '--vanilla', prepare], 'resolving_dependencies')
        from urllib.parse import urlsplit
        resolved = json.loads(lock.read_text())
        # CRAN metadata may contain folded whitespace around SHA256. pak's
        # pinned-version cache lookup compares it literally; normalize only
        # whitespace while retaining the original resolver record.
        original_lock = self.folder / 'r-resolved.lock'
        if not original_lock.exists():
            shutil.copyfile(lock, original_lock)
        for package in resolved.get('packages', []):
            if package.get('sha256'):
                checksum = package['sha256'].strip()
                if not re.fullmatch('[0-9a-fA-F]{64}', checksum):
                    raise EnvironmentError('Invalid R package artifact checksum')
                package['sha256'] = checksum.lower()
        write_json(lock, resolved)
        pending = [resolved]
        while pending:
            node = pending.pop()
            if isinstance(node, dict):
                pending.extend(node.values())
            elif isinstance(node, list):
                pending.extend(node)
            elif isinstance(node, str) and node.startswith(('http:', 'https:')):
                url = urlsplit(node)
                if url.scheme != 'https' or url.username or url.password or not (
                    url.hostname in {'cloud.r-project.org', 'cran.r-project.org', 'cran.rstudio.com', 'mac.cran.dev', 'bioconductor.org'}
                    or (url.hostname or '').endswith('.bioconductor.org')):
                    raise EnvironmentError('Resolved R dependency introduces an unapproved source')
        download = self.folder / 'download_packages.R'
        # Do not pass author source trees to an online download operation:
        # pkg_download may package local trees. Only frozen registry artifacts
        # belong here; local source builds happen in the offline installer.
        registry_refs = [p['package'] + '@' + p['version'] for p in resolved.get('packages', [])
                         if p.get('type') == 'standard']
        download_vector = 'c(' + ','.join(q(v) for v in registry_refs) + ')'
        download.write_text('options(repos=c(CRAN="https://cloud.r-project.org"), timeout=1800, pkg.sysreqs=FALSE)\n'
            + 'pak::pkg_download(' + download_vector + ',dest_dir=' + q(str(self.folder / 'r-downloads')) + ',dependencies=FALSE)\n', encoding='utf-8')
        (self.folder / 'r-downloads').mkdir(exist_ok=True)
        if registry_refs:
            self.run([rscript, '--vanilla', download], 'downloading_packages')
        write_json(self.folder / 'r.artifacts.json', inventory(self.folder / 'r-downloads'))
        # Bind installation to the exact downloaded files, not pak's mutable
        # cache freshness state (which may otherwise request network offline).
        offline = copy.deepcopy(resolved)
        local_refs = {}
        for package in offline.get('packages', []):
            if package.get('type') != 'standard':
                continue
            from .planning_evidence import _path
            archive = _path(self.folder / 'r-downloads', package['target'])
            if not archive.is_file():
                raise EnvironmentError('Locked R artifact was not downloaded: ' + package['package'])
            if package.get('sha256') and digest(archive) != package['sha256']:
                raise EnvironmentError('Locked R artifact checksum mismatch: ' + package['package'])
            reference = 'local::' + str(archive)
            local_refs[package['ref']] = reference
            package.update(ref=reference, type='local', sources=[str(archive)], sha256=digest(archive))
        for package in offline.get('packages', []):
            package['dependencies'] = [local_refs.get(ref, ref) for ref in package.get('dependencies', [])]
            for dependency in package.get('deps', []):
                dependency['ref'] = local_refs.get(dependency['ref'], dependency['ref'])
        offline_lock = self.folder / 'r-install.lock'
        write_json(offline_lock, offline)
        install = self.folder / 'install_packages.R'
        install.write_text('options(Ncpus=2, pkg.sysreqs=FALSE)\n'
            'pak::lockfile_install(lockfile=' + q(str(offline_lock)) + ',lib=' + q(str(library)) + ',update=FALSE)\n', encoding='utf-8')
        self.run([rscript, '--vanilla', install], 'installing_packages', True)
        if json.loads((self.folder / 'r.artifacts.json').read_text()) != inventory(self.folder / 'r-downloads'):
            raise EnvironmentError('Downloaded R artifacts changed during installation')
        return str(library), {'r-packages.lock': digest(lock), 'r-resolved.lock': digest(original_lock), 'r-install.lock': digest(offline_lock)}


def prepare(root, plan, approved_hash, cfg, progress=None, *, installer=Installer, probe=None):
    """Prepare or reuse a sealed environment. Failed attempts never become ready."""
    from .planning_evidence import _path, verify_planning_evidence, probe_method_apis
    from .actions import rscript_path
    progress = progress or (lambda stage: None)
    spec = approved_spec(plan, approved_hash)
    bundle = verify_planning_evidence(root, plan, stage='execute', cfg=cfg, reprobe=False)
    key = canonical_hash({'spec': spec, 'platform': platform.platform(), 'python': digest(sys.executable),
                          'r': digest(shutil.which(rscript_path(root)) or rscript_path(root)) if 'r' in spec['interpreters'] else None})
    base = _path(Path(root), '.runtime/task_environments/' + key)
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Kernel lock is released automatically on crash; the environment never moves
    # after venv creation, so interpreter launchers retain correct absolute paths.
    from .platform_support import lock_fd
    with (base / 'install.lock').open('a+b') as lease:
        lock_fd(lease.fileno(), blocking=False)
        for ready in sorted(base.glob('attempt_*/receipt.json')):
            receipt = json.loads(ready.read_text())
            verify_receipt(root, plan, receipt)
            progress('reusing_environment')
            return receipt
        if cfg.get('planning', {}).get('evidence', {}).get('offline', False):
            raise EnvironmentError('Offline mode has no verified matching task environment; no downloads attempted')
        folder = base / ('attempt_' + str(time.time_ns()))
        folder.mkdir(exist_ok=True, mode=0o700)
        state = folder / 'transaction.json'
        write_json(state, {'status': 'preparing', 'plan_hash': approved_hash})
        try:
            backend = installer(root, folder, cfg, progress)
            python_deps = [d for d in spec['dependencies'] if d['language'] == 'python']
            r_deps = [d for d in spec['dependencies'] if d['language'] == 'r']
            receipt = {'directory': folder.relative_to(root).as_posix(), 'spec_hash': canonical_hash(spec),
                       'platform': platform.platform(), 'spec': spec, 'python': None, 'r_library': None, 'locks': {}}
            if python_deps:
                receipt['python'], locks = backend.python(python_deps)
                receipt['locks'].update(locks)
            if r_deps:
                receipt['r_library'], locks = backend.r(r_deps)
                receipt['locks'].update(locks)
            current_cfg = bound_config(cfg, receipt)
            selected = {b['id'] for b in plan['method_bindings']}
            candidates = [r['candidate'] for r in bundle['methods'] if r['candidate']['id'] in selected]
            progress('verifying_environment')
            work = folder / ('probe_' + uuid.uuid4().hex)
            work.mkdir()
            rows = (probe or probe_method_apis)(Path(root), candidates, current_cfg, work)
            if any(r.get('runtime') != spec['interpreters'].get(r['language']) for r in rows):
                raise EnvironmentError('Installed interpreter differs from approved version')
            if {r['id'] for r in rows} != selected or any(r['status'] != 'verified' for r in rows):
                raise EnvironmentError('Installed method API verification failed; re-plan if a method must change')
            versions = {(d['language'], d['package']): d['version'] for d in spec['dependencies']}
            for row in rows:
                expected = versions.get((row['language'], row['package']))
                if expected and row['version'] != expected:
                    raise EnvironmentError('Installed package differs from approved version')
            for record in [*folder.glob('*.bootstrap.json'), *folder.glob('*.artifacts.json'), *folder.glob('sources/*/source.json'), *folder.glob('sources/*/source.tar.gz')]:
                receipt['locks'][record.relative_to(folder).as_posix()] = digest(record)
            receipt.update(apis=rows, interpreters={r['interpreter']: digest(r['interpreter']) for r in rows},
                           files=inventory(folder / 'runtime'))
            write_json(folder / 'receipt.json', receipt)
            write_json(state, {'status': 'ready', 'plan_hash': approved_hash})
            return receipt
        except BaseException as exc:
            write_json(state, {'status': 'failed', 'error': str(exc), 'plan_hash': approved_hash})
            raise

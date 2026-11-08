"""Source-bound pre-plan evidence. Models propose candidates, never verification receipts.

All statistical operations remain mature package calls. This module implements
project-specific retrieval, provenance and lifecycle rules, not statistical methods.
"""
from __future__ import annotations

import ast
import gzip
import hashlib
import io
import ipaddress
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from email.parser import Parser

import jsonschema
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name

from orchestrator.privacy import sensitive_matches
from orchestrator.research_literature import LiteratureService, _digest, _normalise, _plain, verify_source_record, record_passages

PROTOCOL = 'method_evidence_v1'
DEFAULTS = {'cache_days': 30, 'offline': False, 'refresh': False, 'timeout': 25, 'retries': 2}
DESIGN_KEYS = ('analysis_unit', 'data_scale', 'pairing', 'covariates', 'missing_values', 'multiple_testing', 'validation')
CANDIDATE_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'required': ['id', 'name', 'language', 'package', 'apis', 'query'],
    'properties': {'id': {'type': 'string', 'pattern': '^[a-z][a-z0-9_]{0,39}$'},
                   'name': {'type': 'string', 'minLength': 1, 'maxLength': 200},
                   'language': {'enum': ['python', 'r']},
                   'package': {'type': 'string', 'pattern': '^[A-Za-z][A-Za-z0-9_.-]{0,99}$'},
                   'apis': {'type': 'array', 'minItems': 1, 'maxItems': 8, 'uniqueItems': True,
                            'items': {'type': 'string', 'pattern': '^[A-Za-z][A-Za-z0-9_.]{0,159}$'}},
                   'query': {'type': 'string', 'minLength': 3, 'maxLength': 500},
                   'required': {'type': 'boolean'},
                   'documentation_url': {'type': 'string', 'maxLength': 1500}}}
PROPOSAL = '''Propose retrieval ONLY; do not construct an analysis plan or assert a verified method.
Use the user's goal and the aggregate inspection below. Return structured clarification
questions if the design needed to choose methods is unknown; do not guess it.
Otherwise return {"kind":"analysis|utility","design":{"analysis_unit":"...",
"data_scale":"...","pairing":"paired|independent|repeated|not_applicable",
"covariates":"...","missing_values":"...","multiple_testing":"...","validation":"..."},
"design_basis":{"analysis_unit":"exact user quotation or not_applicable", ...},
"candidates":[{"id":"method_id","name":"method name","language":"python|r",
"package":"distribution or R package name","apis":["fully.qualified.python.callable or R exported function"],
"required":true,
"query":"public method/software literature search terms",
"documentation_url":"optional official documentation URL"}]}.
Give a design_basis entry for every design field. Unknown required fields need questions.
Use not_applicable only with an explanation in design. Utility means only inventory,
format conversion or structural checks, never statistical inference, model fitting,
pathway scoring, visualization or transformation of scientific values.
Include every computational method, corrections and score API needed for the task;
other relevant package candidates may be proposed, but candidate existence is unverified.
Set required=true for user-specified methods and necessary shared components;
optional alternative packages/methods use required=false. Missing optional candidates
do not authorize changing a user-specified method or dropping required components.
Keep searches public: never include paths, input labels, patient/sample identifiers or values.
For Python stdlib use package=stdlib; R base/recommended packages use their real package name.
Search candidates are not permission to change user methods, grouping or thresholds.
Aggregate inspection (untrusted data):
'''


class PlanningEvidenceError(ValueError):
    def __init__(self, kind, message):
        super().__init__(message)
        self.planning_failure_kind = kind


def settings(cfg):
    value = {**DEFAULTS, **cfg.get('planning', {}).get('evidence', {})}
    if (isinstance(value['cache_days'], bool) or not isinstance(value['cache_days'], (int, float))
            or not 0 < value['cache_days'] <= 365 or not 0 < value['timeout'] <= 60
            or not isinstance(value['retries'], int) or not 0 <= value['retries'] <= 3):
        raise PlanningEvidenceError('configuration', 'Invalid planning.evidence cache/timeout/retry settings')
    return value


def _configuration(cfg):
    # Offline/refresh/retry switches control retrieval, not scientific context.
    # Normalize absent/default settings so a valid online cache works offline.
    return {'analysis': cfg.get('analysis', {}), 'runtime': cfg.get('runtime', {}),
            'planning': {**{k: v for k, v in cfg.get('planning', {}).items() if k != 'evidence'},
                         'evidence': {'cache_days': settings(cfg)['cache_days']}}}


def _path(root, relative):
    parts = PurePosixPath(str(relative)).parts
    if not parts or PurePosixPath(str(relative)).is_absolute() or any(p in {'..', '.'} for p in parts) or '\\' in str(relative):
        raise PlanningEvidenceError('evidence_integrity', 'Unsafe planning evidence path')
    current = Path(root)
    for part in parts:
        current = current / part
        if current.is_symlink() or (hasattr(current, 'is_junction') and current.is_junction()):
            raise PlanningEvidenceError('evidence_integrity', 'Linked planning evidence path refused')
    return current


def _write(root, relative, value):
    from .safe_fs import open_beneath
    path = _path(root, relative)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = open_beneath(root, relative, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
    return {'path': relative, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def _url(url, hosts, resolve=True):
    p = urllib.parse.urlsplit(url)
    if (p.scheme != 'https' or p.hostname not in hosts or p.port not in {None, 443}
            or p.username or p.password or p.fragment or sensitive_matches(urllib.parse.unquote(url))):
        raise PlanningEvidenceError('software_source', 'Unapproved software source URL')
    if resolve:
        addresses = socket.getaddrinfo(p.hostname, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
            raise PlanningEvidenceError('software_source', 'Non-public software endpoint refused')


def _http(url, hosts, timeout):
    _url(url, hosts)
    class Redirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            _url(newurl, hosts)
            return super().redirect_request(req, fp, code, msg, headers, newurl)
    with urllib.request.build_opener(Redirect()).open(
            urllib.request.Request(url, headers={'User-Agent': 'tme-agent-method-evidence/1.0'}), timeout=timeout) as response:
        _url(response.url, hosts)
        content = response.read(12 * 1024 * 1024 + 1)
    if len(content) > 12 * 1024 * 1024:
        raise PlanningEvidenceError('software_source', 'Software response exceeds size limit')
    return content


def retrieve_software_sources(candidate, environment, cfg, transport=None):
    """Retrieve registry metadata and official docs, never install or execute them."""
    s = settings(cfg)
    snapshots = []
    def get(url, hosts):
        _url(url, hosts, resolve=False)
        for attempt in range(s['retries'] + 1):
            try:
                data = transport(url, timeout=s['timeout']) if transport else _http(url, hosts, s['timeout'])
                if not isinstance(data, bytes) or len(data) > 12 * 1024 * 1024:
                    raise ValueError('Invalid software response')
                snapshots.append({'url': url, 'sha256': _digest(data), 'content_hex': data.hex()})
                return data
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                if attempt == s['retries'] or (isinstance(exc, urllib.error.HTTPError) and exc.code not in {429} and exc.code < 500):
                    raise
                time.sleep(min(2 ** attempt, 4))
    package, version = candidate['package'], environment.get('version', 'unavailable')
    if environment.get('status') != 'verified':
        # Search official sources even when the environment lacks the method;
        # discovery is retained separately and can never become an API receipt.
        if candidate['language'] == 'python':
            if package == 'stdlib':
                url = 'https://docs.python.org/3/library/' + candidate['apis'][0].split('.')[0] + '.html'
                get(url, {'docs.python.org'})
                discovered = {'name': package, 'version': version}
            else:
                url = 'https://pypi.org/pypi/' + urllib.parse.quote(package, safe='') + '/json'
                discovered = json.loads(get(url, {'pypi.org'}))['info']
                if canonicalize_name(discovered['name']) != canonicalize_name(package):
                    raise PlanningEvidenceError('software_source', 'Registry candidate identity mismatch')
                discovered = {k: discovered.get(k) for k in ('name', 'version', 'requires_python', 'requires_dist', 'project_urls')}
        else:
            discovered = None
            for url in ('https://cran.r-project.org/src/contrib/PACKAGES.gz',
                        'https://bioconductor.org/packages/release/bioc/src/contrib/PACKAGES.gz'):
                data = gzip.GzipFile(fileobj=io.BytesIO(get(url, {urllib.parse.urlsplit(url).hostname}))).read(24 * 1024 * 1024 + 1)
                if len(data) > 24 * 1024 * 1024:
                    raise PlanningEvidenceError('software_source', 'Registry catalog exceeds size limit')
                records = [Parser().parsestr(part) for part in data.decode().split('\n\n') if part.strip()]
                matched = [dict(record) for record in records if record.get('Package') == package]
                if matched:
                    discovered = matched[0]
                    break
            if discovered is None:
                raise PlanningEvidenceError('software_source', 'Candidate absent from official R catalogs')
        return {'verification_status': 'dependency_missing', 'registry_url': url, 'metadata': discovered,
                'snapshots': snapshots, 'version': version, 'documentation': '',
                'limitation': 'Official discovery only. Installation, compatibility and actual APIs are not verified.'}
    documentation = '\n\n'.join(row['documentation'] for row in environment['apis'].values())
    if not documentation.strip():
        raise PlanningEvidenceError('api_incompatible', 'API has no readable installed version documentation')
    if candidate['language'] == 'python' and package == 'stdlib':
        module = candidate['apis'][0].split('.')[0]
        if module not in sys.stdlib_module_names:
            raise PlanningEvidenceError('software_source', 'Claimed stdlib API is not standard library')
        url = f'https://docs.python.org/{sys.version_info.major}.{sys.version_info.minor}/library/{module}.html'
        metadata = {'name': 'stdlib', 'version': version, 'official_documentation': url}
        get(url, {'docs.python.org'})
    elif candidate['language'] == 'python':
        url = 'https://pypi.org/pypi/' + urllib.parse.quote(package, safe='') + '/' + urllib.parse.quote(version, safe='') + '/json'
        payload = json.loads(get(url, {'pypi.org'}))
        info = payload['info']
        if canonicalize_name(info['name']) != canonicalize_name(package) or info['version'] != version:
            raise PlanningEvidenceError('software_source', 'PyPI project/version mismatch')
        if info.get('requires_python') and not SpecifierSet(info['requires_python']).contains(environment['runtime'], prereleases=True):
            raise PlanningEvidenceError('api_incompatible', 'Official Python version requirement is not satisfied')
        releases = payload.get('urls', [])
        if not releases or all(row.get('yanked') for row in releases):
            raise PlanningEvidenceError('software_source', 'No non-yanked release artifacts')
        metadata = {key: info.get(key) for key in ('name', 'version', 'requires_python', 'requires_dist', 'project_urls', 'classifiers', 'summary')}
        metadata['release_artifacts'] = [{k: row.get(k) for k in ('filename', 'digests', 'upload_time_iso_8601', 'yanked')} for row in releases]
        links = info.get('project_urls') or {}
        official = [value for key, value in links.items() if re.search('doc', key, re.I)]
        requested = candidate.get('documentation_url')
        if requested:
            # Model URLs must descend from a registry-declared documentation root.
            p = urllib.parse.urlsplit(requested)
            if any(part == '..' for part in urllib.parse.unquote(p.path).split('/')) or not any(
                    p.scheme == 'https' and p.netloc == urllib.parse.urlsplit(base).netloc and
                    p.path.startswith(urllib.parse.urlsplit(base).path.rstrip('/') + '/') for base in official):
                raise PlanningEvidenceError('software_source', 'Documentation URL is not rooted in registry metadata')
            doc_url = requested
        else:
            doc_url = official[0] if official else ''
        if not doc_url:
            raise PlanningEvidenceError('software_source', 'Registry has no official documentation link')
        get(doc_url, {urllib.parse.urlsplit(doc_url).hostname})
    else:
        metadata = environment.get('metadata', {})
        if package in {'base', 'stats', 'utils', 'tools', 'grDevices', 'graphics', 'methods', 'datasets'}:
            url = 'https://stat.ethz.ch/R-manual/R-release/library/' + package + '/html/00Index.html'
            get(url, {'stat.ethz.ch'})
        elif 'bioconductor' in str(metadata).lower() or environment.get('bioconductor_package'):
            release = environment.get('bioconductor')
            if not release or not re.fullmatch(r'\d+\.\d+', release):
                raise PlanningEvidenceError('software_source', 'Cannot resolve the installed Bioconductor release')
            url = f'https://bioconductor.org/packages/{release}/bioc/html/{package}.html'
            catalog_url = f'https://bioconductor.org/packages/{release}/bioc/src/contrib/PACKAGES.gz'
            catalog_data = gzip.GzipFile(fileobj=io.BytesIO(get(catalog_url, {'bioconductor.org'}))).read(24 * 1024 * 1024 + 1)
            if len(catalog_data) > 24 * 1024 * 1024:
                raise PlanningEvidenceError('software_source', 'Bioconductor catalog exceeds size limit')
            catalog = [Parser().parsestr(entry) for entry in catalog_data.decode().split('\n\n') if entry.strip()]
            matching = [dict(entry) for entry in catalog if entry.get('Package') == package and entry.get('Version') == version]
            if not matching:
                raise PlanningEvidenceError('software_source', 'Package/version absent from official Bioconductor catalog')
            metadata = {**metadata, 'official_catalog_match': matching[0]}
            page = _plain(get(url, {'bioconductor.org'}).decode('utf-8'))
            if not re.search(r'(?<![\d.])' + re.escape(version) + r'(?![\d.])', page):
                raise PlanningEvidenceError('software_source', 'Bioconductor page does not verify installed version')
            get(f'https://bioconductor.org/packages/{release}/bioc/manuals/{package}/man/{package}.pdf', {'bioconductor.org'})
        else:
            catalog_url = 'https://cran.r-project.org/src/contrib/PACKAGES.gz'
            catalog_data = gzip.GzipFile(fileobj=io.BytesIO(get(catalog_url, {'cran.r-project.org'}))).read(24 * 1024 * 1024 + 1)
            if len(catalog_data) > 24 * 1024 * 1024:
                raise PlanningEvidenceError('software_source', 'CRAN catalog exceeds size limit')
            catalog = [Parser().parsestr(entry) for entry in catalog_data.decode().split('\n\n') if entry.strip()]
            matches = [dict(entry) for entry in catalog if entry.get('Package') == package]
            if not matches:
                raise PlanningEvidenceError('software_source', 'Package absent from official CRAN catalog')
            url = f'https://cran.r-project.org/web/packages/{package}/index.html'
            page = get(url, {'cran.r-project.org'}).decode('utf-8')
            if f"{package}_{matches[0]['Version']}.tar.gz" not in page:
                raise PlanningEvidenceError('software_source', 'CRAN package identity mismatch')
            if matches[0]['Version'] != version:
                archive = f'https://cran.r-project.org/src/contrib/Archive/{package}/'
                if f'{package}_{version}.tar.gz' not in get(archive, {'cran.r-project.org'}).decode('utf-8'):
                    raise PlanningEvidenceError('software_source', 'CRAN does not verify installed version')
            get(f'https://cran.r-project.org/web/packages/{package}/{package}.pdf', {'cran.r-project.org'})
            metadata = {'installed': metadata, 'registry_page': _plain(page), 'official_catalog_match': matches[0]}
    maintenance = {'status': 'unknown', 'limitations': 'Availability, age and self-reported classifiers are not quality guarantees.'}
    links = metadata.get('project_urls', {}) if isinstance(metadata, dict) else {}
    repo_urls = [str(v) for v in (links or {}).values() if str(v).startswith('https://github.com/')]
    if candidate['language'] == 'r':
        repo_urls += re.findall(r'https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', str(metadata))
    if repo_urls:
        parts = urllib.parse.urlsplit(repo_urls[0]).path.strip('/').split('/')
        if len(parts) >= 2:
            api_url = 'https://api.github.com/repos/' + parts[0] + '/' + parts[1].removesuffix('.git')
            try:
                repo = json.loads(get(api_url, {'api.github.com'}))
                if str(repo.get('full_name', '')).lower() != (parts[0] + '/' + parts[1].removesuffix('.git')).lower():
                    raise PlanningEvidenceError('software_source', 'Official repository identity mismatch')
                maintenance = {k: repo.get(k) for k in ('archived', 'disabled', 'pushed_at', 'updated_at', 'license', 'open_issues_count')}
                maintenance['status'] = 'archived' if repo.get('archived') or repo.get('disabled') else 'repository_metadata_verified'
            except (OSError, ValueError, KeyError) as exc:
                maintenance['retrieval_error'] = type(exc).__name__
    return {'registry_url': url, 'metadata': metadata, 'documentation': documentation,
            'documentation_origin': 'installed_package_api', 'version': version,
            'snapshots': snapshots, 'maintenance': maintenance, 'semantic_review': 'required',
            'maintenance_limits': 'Registry and documentation availability do not prove maintenance quality or scientific applicability.'}


def probe_method_apis(root, candidates, cfg, folder):
    """Use actual task interpreters with existing native isolation and library rules."""
    from .task_runner import _step_environment
    from .input_inspection import _run_guarded
    from .actions import rscript_path
    from .temp_paths import cleanup
    rows = []
    for language in ('python', 'r'):
        chosen = [c for c in candidates if c['language'] == language]
        if not chosen:
            continue
        work = folder / ('probe_' + language + '_' + uuid.uuid4().hex)
        _path(root, work.relative_to(root).as_posix()).mkdir(mode=0o700)
        suffix = '.py' if language == 'python' else '.R'
        script = work / ('probe' + suffix)
        script.write_bytes(Path(__file__).with_name('planning_probe' + suffix).read_bytes())
        request, output = work / 'request.json', work / 'environment.json'
        request.write_text(json.dumps(chosen), encoding='utf-8')
        env = _step_environment(root, work, cfg, [])
        interpreter = sys.executable if language == 'python' else rscript_path(root)
        argv = [sys.executable, str(Path(__file__).with_name('task_guard.py')), str(work), '--', interpreter]
        if language == 'r':
            argv.append('--vanilla')
        argv += [str(script), str(request), str(output)]
        try:
            result = _run_guarded(root, work, argv, env)
            (work / 'probe.log').write_text(result.stdout + result.stderr, encoding='utf-8')
            if result.returncode or not output.is_file():
                raise PlanningEvidenceError('api_incompatible', 'API probe failed; inspect ' + work.relative_to(root).as_posix())
            rows.extend(json.loads(output.read_text(encoding='utf-8')))
        finally:
            cleanup(root, work.name, task_dir=work)
    return rows


def _proposal_valid(proposal, request, private_ids=()):
    if not isinstance(proposal, dict):
        raise ValueError('Method search proposal must be an object')
    if proposal.get('kind') not in {'analysis', 'utility'}:
        raise ValueError('Evidence proposal requires kind=analysis|utility')
    candidates = proposal.get('candidates')
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= 8:
        raise ValueError('Propose 1-8 method/software candidates')
    ids = set()
    for row in candidates:
        jsonschema.validate(row, CANDIDATE_SCHEMA)
        if row['id'] in ids:
            raise ValueError('Duplicate method candidate ID')
        ids.add(row['id'])
        if row.get('required') is False:
            named = [row['name'], row['package'], *(a.split('.')[-1] for a in row['apis'])]
            if any(len(term) >= 4 and re.search(r'(?<![A-Za-z0-9_])' + re.escape(term) + r'(?![A-Za-z0-9_])', request, re.I) for term in named):
                raise PlanningEvidenceError('evidence_validation', 'An explicitly named method/package cannot be optional')
        if sensitive_matches(json.dumps(row), private_ids):
            raise PlanningEvidenceError('privacy', 'Private content in method retrieval proposal')
        if row['language'] == 'python' and any('.' not in api for api in row['apis']):
            raise ValueError('Python APIs must be fully qualified')
    if proposal['kind'] == 'utility' and re.search(
            r'GSVA|ssgsea|plage|wilcox|differential|regression|survival|prediction|enrichment|通路评分|差异分析|回归|生存|预测|富集|绘图|画图', request, re.I):
        raise PlanningEvidenceError('design_conflict', 'Scientific request cannot bypass evidence as utility')
    if proposal['kind'] == 'utility':
        utility = {
            'python': {'csv.DictReader', 'csv.DictWriter', 'json.load', 'json.dump', 'pathlib.Path',
                       'numpy.isfinite', 'pandas.read_csv', 'pandas.read_table', 'pandas.DataFrame.info',
                       'pandas.DataFrame.to_csv'},
            'r': {'readRDS', 'saveRDS', 'dim', 'nrow', 'ncol', 'names', 'colnames', 'rownames', 'str',
                  'is.finite', 'anyNA', 'read.csv', 'read.delim', 'write.csv', 'write.table', 'file.info', 'list.files'}}
        for c in candidates:
            if not set(c['apis']) <= utility[c['language']]:
                raise PlanningEvidenceError('design_conflict', 'Utility tasks are limited to structural and file APIs; scientific methods require literature')
    design, basis = proposal.get('design', {}), proposal.get('design_basis', {})
    for key in DESIGN_KEYS:
        if not isinstance(design.get(key), str) or not design[key].strip():
            raise PlanningEvidenceError('design_unknown', 'Clarify design field: ' + key)
        quote = basis.get(key)
        if not isinstance(quote, str) or (quote != 'not_applicable' and (not quote or quote not in request)):
            raise PlanningEvidenceError('design_unknown', 'Design needs an exact user basis: ' + key)
        if quote == 'not_applicable' and design[key] != 'not_applicable':
            raise PlanningEvidenceError('design_unknown', 'Missing design cannot be relabelled not_applicable: ' + key)
    if proposal['kind'] == 'analysis' and any(design[k] == 'not_applicable' for k in ('analysis_unit', 'data_scale', 'pairing')):
        raise PlanningEvidenceError('design_unknown', 'Analysis unit, scale and dependence structure must be explicit')
    if re.search(r'(?<!un)paired|配对', request, re.I) and not re.search(r'unpaired|非配对', request, re.I) and design['pairing'] != 'paired':
        raise PlanningEvidenceError('design_conflict', 'User requested paired analysis')


def collect_planning_evidence(client, root, request, inspection, cfg, *, task_id=None,
                              literature_service=None, software_transport=None, probe=None, source_request=None):
    """Retrieve and freeze evidence before the final planner can be invoked."""
    from .input_inspection import public_inspection
    from .task_sessions import TaskStore
    root = Path(root).resolve()
    s = settings(cfg)
    scope = str(TaskStore(root).directory(task_id).relative_to(root)) if task_id else '.runtime/planning_evidence'
    attempt = scope + '/planning/attempt_' + uuid.uuid4().hex
    _path(root, attempt).mkdir(parents=True, mode=0o700)
    if task_id:
        state = TaskStore(root).get(task_id)
        state.update(planning_attempt=attempt, planning_stage='method_search')
        TaskStore(root).save(state)
    context = {'request_sha256': _digest((source_request if source_request is not None else request).encode()), 'inspection': public_inspection(inspection),
               'configuration': _configuration(cfg)}
    _write(root, attempt + '/context.json', context)
    try:
        from .task_planner import CLARIFICATIONS
        from orchestrator import pathway_scoring
        frozen_scoring = pathway_scoring.draft_protocol(request, cfg)
        search_system = PROPOSAL + json.dumps(public_inspection(inspection), ensure_ascii=False) + '\n' + CLARIFICATIONS
        if frozen_scoring is not None:
            search_system += ('\nIf pathway scoring is requested, retain this local frozen protocol. Include '
                              'GSVA::gsva and every named parameter constructor among the candidate R APIs.\n' +
                              pathway_scoring.prompt_block(frozen_scoring))
        def validate(value):
            if 'questions' in value:
                from .task_planner import validate_plan
                validate_plan(root, value, source_request or request, cfg, inspection=inspection)
                if any(isinstance(q, str) for q in value['questions']):
                    raise ValueError('New method clarifications require structured clickable options')
                return
            _proposal_valid(value, request, getattr(client, 'private_ids', ()))
        proposal = client.chat_json('tui_method_search', search_system,
                                    request, 'tui_method_search_v1', validator=validate)['parsed']
        validate(proposal)
        if 'questions' in proposal:
            _write(root, attempt + '/questions.json', proposal)
            return proposal
        _write(root, attempt + '/proposal.json', proposal)
        environment = (probe or probe_method_apis)(root, proposal['candidates'], cfg, _path(root, attempt))
        _write(root, attempt + '/environment.json', environment)
        found = {row['id']: row for row in environment}
        if set(found) != {c['id'] for c in proposal['candidates']}:
            raise PlanningEvidenceError('api_incompatible', 'Incomplete method environment probe')
        # Version/signature changes produce a new cache key; raw data never enters it.
        cache_key = _digest({'protocol': PROTOCOL, 'context': context, 'proposal': proposal, 'environment': environment})
        cache_dir = '.cache/planning_evidence/' + cache_key
        cached = None
        cache_errors = []
        if not s['refresh']:
            for path in sorted(_path(root, cache_dir).glob('*.json'), reverse=True):
                try:
                    value = json.loads(_path(root, path.relative_to(root).as_posix()).read_text(encoding='utf-8'))
                    digest = value.pop('sha256')
                    age = (datetime.now(timezone.utc) - datetime.fromisoformat(value['retrieved_at'])).total_seconds()
                    if _digest(value) != digest:
                        cache_errors.append({'kind': 'evidence_integrity', 'path': path.relative_to(root).as_posix()})
                        continue
                    if not 0 <= age <= s['cache_days'] * 86400:
                        cache_errors.append({'kind': 'evidence_expired', 'path': path.relative_to(root).as_posix()})
                        continue
                    cached = value
                    break
                except (OSError, ValueError, KeyError, TypeError):
                    cache_errors.append({'kind': 'evidence_integrity', 'path': path.relative_to(root).as_posix()})
                    continue
        _write(root, attempt + '/cache_validation.json', cache_errors)
        if cached is None and s['offline']:
            raise PlanningEvidenceError(cache_errors[0]['kind'] if cache_errors else 'evidence_offline', 'No intact, fresh, version-matching method evidence cache')
        if cached is not None and (not cached['errors'] or s['offline']):
            bundle = {**cached, 'cache_hit': True}
        else:
            rows, errors = [], []
            previous = {r['candidate']['id']: r for r in (cached or {}).get('methods', [])}
            service = literature_service or LiteratureService(root / '.cache/research_literature',
                {**cfg.get('research', {}).get('literature', {}), 'offline': False, 'refresh': True,
                 'max_queries': 8, 'timeout': s['timeout'], 'retries': s['retries']})
            for candidate in proposal['candidates']:
                if task_id:
                    state = TaskStore(root).get(task_id)
                    state['planning_stage'] = 'method_sources:' + candidate['id']
                    TaskStore(root).save(state)
                env = found[candidate['id']]
                if candidate['id'] in previous:
                    rows.append(previous[candidate['id']])
                    continue
                if env.get('status') != 'verified':
                    entry = {'id': candidate['id'], 'kind': 'dependency' if env.get('status') == 'missing' else 'api_incompatible',
                             'package': candidate['package'], 'language': candidate['language'],
                             'error': env.get('error', 'API missing')}
                    try:
                        entry['software_discovery'] = retrieve_software_sources(candidate, env, cfg, software_transport)
                        entry['literature_discovery'] = service.search([candidate['query']], context={'private_ids': getattr(client, 'private_ids', ())})
                    except (OSError, ValueError, KeyError) as exc:
                        entry['discovery_error'] = type(exc).__name__
                    errors.append(entry)
                    continue
                try:
                    software = retrieve_software_sources(candidate, env, cfg, software_transport)
                    literature = service.search([candidate['query']], context={'private_ids': getattr(client, 'private_ids', ())}) if proposal['kind'] == 'analysis' else {'records': [], 'status': 'not_required', 'queries': []}
                    for record in literature['records']:
                        verify_source_record(record)
                    literature['raw_responses'] = []
                    for query in literature.get('queries', []):
                        if not isinstance(query, dict):
                            continue
                        for snap in query.get('response_snapshots', []):
                            source = _path(root, str(Path(snap['path']).relative_to(root)))
                            data = source.read_bytes()
                            if _digest(data) != snap['sha256']:
                                raise PlanningEvidenceError('evidence_integrity', 'Literature response snapshot changed')
                            literature['raw_responses'].append({'url': snap['url'], 'sha256': snap['sha256'], 'content_hex': data.hex()})
                    if proposal['kind'] == 'analysis' and not literature['records']:
                        raise PlanningEvidenceError('literature_missing', 'No verified methodology source: ' + literature['status'])
                    rows.append({'candidate': candidate, 'environment': env, 'software': software, 'literature': literature})
                except (OSError, ValueError, KeyError) as exc:
                    errors.append({'id': candidate['id'], 'kind': getattr(exc, 'planning_failure_kind', 'software_source'), 'error': type(exc).__name__ + ': ' + str(exc)})
            bundle = {'protocol': PROTOCOL, 'context': context, 'proposal': proposal, 'methods': rows,
                      'errors': errors, 'retrieved_at': datetime.now(timezone.utc).isoformat(), 'cache_hit': False}
            # Reusing one successful stage must not renew its retrieval age.
            if previous:
                bundle['retrieved_at'] = min(bundle['retrieved_at'], cached['retrieved_at'])
            _write(root, cache_dir + '/' + str(time.time_ns()) + '.json', {**bundle, 'sha256': _digest(bundle)})
        _write(root, attempt + '/dependency_requirements.json', [e for e in bundle['errors'] if e['kind'] in {'dependency', 'api_incompatible'}])
        artifact_names = [p.relative_to(root).as_posix() for p in _path(root, attempt).rglob('*')
                          if p.is_file() and not {'tmp', '.cache'}.intersection(p.relative_to(_path(root, attempt)).parts)]
        bundle['artifacts'] = [{'path': name, 'sha256': _digest(_path(root, name).read_bytes())} for name in sorted(artifact_names)]
        receipt = _write(root, attempt + '/bundle.json', bundle)
        required = {c['id'] for c in proposal['candidates'] if c.get('required', True)}
        unavailable = required - {r['candidate']['id'] for r in bundle['methods']}
        if unavailable:
            error = next((e for e in bundle['errors'] if e['id'] in unavailable), {'kind': 'literature_missing'})
            raise PlanningEvidenceError(error['kind'], 'Required methods unavailable: ' + ', '.join(sorted(unavailable)) + '; inspect ' + attempt + '/bundle.json')
        if not bundle['methods']:
            kind = bundle['errors'][0]['kind'] if bundle['errors'] else 'literature_missing'
            raise PlanningEvidenceError(kind, 'No candidate passed method evidence checks; inspect ' + attempt + '/bundle.json')
        software = [{'id': r['candidate']['id'], 'package': r['candidate']['package'],
                     'version': r['environment']['version'], 'apis': r['candidate']['apis']} for r in bundle['methods']]
        return {'protocol': PROTOCOL, **receipt, 'cache_days': s['cache_days'], 'cache_hit': bundle['cache_hit'],
                'retrieved_at': bundle['retrieved_at'], 'software': software, 'bundle': bundle}
    except Exception as exc:
        _write(root, attempt + '/failure.json', {'kind': getattr(exc, 'planning_failure_kind', 'evidence_validation'),
                                               'error': type(exc).__name__, 'message': str(exc)})
        if task_id:
            state = TaskStore(root).get(task_id)
            state['planning_stage'] = 'blocked'
            TaskStore(root).save(state)
        raise


def validate_method_bindings(bindings, bundle, plan):
    """Check citations and design constraints, never claim semantic entailment."""
    if not isinstance(bindings, list) or not bindings:
        raise PlanningEvidenceError('evidence_validation', 'Final plan must bind verified method IDs')
    methods = {row['candidate']['id']: row for row in bundle['methods']}
    if plan.get('analysis_unit') != bundle['proposal']['design']['analysis_unit']:
        raise PlanningEvidenceError('design_conflict', 'Final plan changed the verified analysis unit')
    ids = set()
    for binding in bindings:
        mid = binding.get('id')
        if mid not in methods or mid in ids:
            raise PlanningEvidenceError('evidence_validation', 'Unknown or duplicate verified method ID')
        ids.add(mid)
        row = methods[mid]
        for key in ('rationale', 'limitations', 'alternatives'):
            if not isinstance(binding.get(key), str) or not binding[key].strip():
                raise PlanningEvidenceError('evidence_validation', 'Missing method assessment: ' + key)
        quote = binding.get('documentation_quote', '')
        if not isinstance(quote, str) or len(quote.strip()) < 16 or _normalise(quote) not in _normalise(row['software']['documentation']):
            raise PlanningEvidenceError('evidence_validation', 'Method requires an exact installed API documentation passage')
        if bundle['proposal']['kind'] == 'analysis':
            citation = binding.get('paper', {})
            sources = {r['source_id']: r for r in row['literature']['records']}
            source = sources.get(citation.get('source_id'))
            passage = citation.get('quote', '')
            if not source or not isinstance(passage, str) or len(passage.strip()) < 16 or not any(
                    _normalise(passage) in _normalise(p['text']) for p in record_passages(source)):
                raise PlanningEvidenceError('evidence_validation', 'Method paper citation is not backed by a frozen passage')
            from orchestrator.research_phases import _is_retracted
            if source.get('retracted') or _is_retracted(source):
                raise PlanningEvidenceError('evidence_conflict', 'Retracted method source cannot support approval')
        requirements = binding.get('requirements', {})
        if set(requirements) != set(DESIGN_KEYS) or any(
                not isinstance(requirements[k], str) or not requirements[k].strip() for k in DESIGN_KEYS):
            raise PlanningEvidenceError('design_unknown', 'Assess all explicit design requirements')
        candidate = row['candidate']
        apis = ' '.join(candidate['apis']).lower()
        pairing = bundle['proposal']['design']['pairing']
        if pairing == 'paired' and re.search(r'mannwhitneyu|ttest_ind|ranksums', apis):
            raise PlanningEvidenceError('design_conflict', 'Independent-sample API in a paired design')
        if pairing == 'independent' and re.search(r'wilcoxon|ttest_rel', apis):
            raise PlanningEvidenceError('design_conflict', 'Paired API in an independent design')
        scale = bundle['proposal']['design']['data_scale'].lower()
        if candidate['package'].lower() in {'deseq2', 'edger'} and scale not in {'counts', 'raw_counts'}:
            raise PlanningEvidenceError('design_conflict', 'Count-based method requires explicit count-scale inputs')
        if candidate['package'].lower() in {'scikit-learn', 'sklearn'} and bundle['proposal']['design']['validation'] == 'not_applicable':
            raise PlanningEvidenceError('design_unknown', 'Predictive methods require explicit within-training preprocessing and subject-level validation')
    if bundle['proposal']['design']['pairing'] == 'paired' and any(
            not b.get('pair_key') for b in plan.get('input_bindings', {}).get('matrices', [])):
        raise PlanningEvidenceError('design_conflict', 'Paired methods require an explicit inspected pair key')
    for step in plan['steps']:
        declared = step.get('method_ids')
        if not isinstance(declared, list) or not declared or not set(declared) <= ids:
            raise PlanningEvidenceError('evidence_validation', 'Every execution step must reference its verified method IDs')
    if set().union(*(set(step['method_ids']) for step in plan['steps'])) != ids:
        raise PlanningEvidenceError('evidence_validation', 'Unbound or unused method assessment')
    required = {c['id'] for c in bundle['proposal']['candidates'] if c.get('required', True)}
    if not required <= ids:
        raise PlanningEvidenceError('evidence_validation', 'A required method was omitted or substituted')
    if plan.get('pathway_protocol'):
        needed = {'gsva'} | {value.split('::')[-1] for value in plan['pathway_protocol']['param_api'].values()}
        checked = {api for mid in ids for api in methods[mid]['candidate']['apis']
                   if methods[mid]['candidate']['language'] == 'r' and methods[mid]['candidate']['package'] == 'GSVA'}
        if not needed <= checked:
            raise PlanningEvidenceError('api_incompatible', 'Frozen GSVA protocol has unverified APIs: ' + ', '.join(sorted(needed - checked)))


def verify_planning_evidence(root, plan, *, stage='read', cfg=None, request=None, reprobe=True):
    """Approval freshness is separate from reproducible execution of a frozen plan."""
    ref = plan.get('planning_evidence')
    if not ref:
        raise PlanningEvidenceError('legacy_plan', 'Legacy plan has no pre-plan evidence; create a new reviewed plan')
    if ref.get('protocol') != PROTOCOL:
        raise PlanningEvidenceError('evidence_integrity', 'Unsupported planning evidence protocol')
    if plan.get('planning_protocol') != PROTOCOL:
        raise PlanningEvidenceError('evidence_integrity', 'Planning protocol marker is missing')
    markers = [p for p in plan.get('parameters', []) if p.get('name') == 'planning_protocol']
    if markers != [{'name': 'planning_protocol', 'value': PROTOCOL, 'source': 'evidence:' + ref.get('sha256', '')}]:
        raise PlanningEvidenceError('evidence_integrity', 'Missing downgrade-protection parameter')
    relative = ref.get('path', '')
    if not (relative.startswith('workspace/tui/sessions/') or relative.startswith('.runtime/planning_evidence/')) or not relative.endswith('/bundle.json'):
        raise PlanningEvidenceError('evidence_integrity', 'Evidence is outside the trusted planning area')
    path = _path(root, relative)
    if hashlib.sha256(path.read_bytes()).hexdigest() != ref.get('sha256'):
        raise PlanningEvidenceError('evidence_integrity', 'Planning evidence changed')
    bundle = json.loads(path.read_text(encoding='utf-8'))
    if bundle.get('protocol') != PROTOCOL:
        raise PlanningEvidenceError('evidence_integrity', 'Invalid evidence bundle protocol')
    if ref.get('retrieved_at', bundle['retrieved_at']) != bundle['retrieved_at']:
        raise PlanningEvidenceError('evidence_integrity', 'Evidence display time differs from source')
    if 'software' in ref and ref['software'] != [{'id': r['candidate']['id'], 'package': r['candidate']['package'],
                'version': r['environment']['version'], 'apis': r['candidate']['apis']} for r in bundle['methods']]:
        raise PlanningEvidenceError('evidence_integrity', 'Evidence display APIs differ from source')
    for artifact in bundle.get('artifacts', []):
        if not artifact['path'].startswith(str(PurePosixPath(relative).parent) + '/') or _digest(_path(root, artifact['path']).read_bytes()) != artifact['sha256']:
            raise PlanningEvidenceError('evidence_integrity', 'Planning attempt artifact changed')
    if request is not None and _digest(request.encode()) != bundle['context']['request_sha256']:
        raise PlanningEvidenceError('evidence_integrity', 'User requirements changed; re-plan')
    if stage in {'plan', 'approve'}:
        days = min(ref.get('cache_days', 30), settings(cfg or {})['cache_days'])
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(bundle['retrieved_at'])).total_seconds()
        if not 0 <= age <= days * 86400:
            raise PlanningEvidenceError('evidence_expired', 'Method evidence expired; re-plan before approval')
    for row in bundle['methods']:
        for record in row['literature']['records']:
            verify_source_record(record)
        for snapshot in row['software']['snapshots'] + row['literature'].get('raw_responses', []):
            if _digest(bytes.fromhex(snapshot['content_hex'])) != snapshot['sha256']:
                raise PlanningEvidenceError('evidence_integrity', 'Software source snapshot hash mismatch')
    source_inputs = {p['path']: p['sha256'] for p in bundle['context']['inspection']['profiles']}
    if any(source_inputs.get(i['path']) != i['sha256'] for i in plan['inputs']):
        raise PlanningEvidenceError('evidence_integrity', 'Plan inputs differ from method evidence inputs')
    if stage in {'approve', 'execute'}:
        from .task_runner import file_hash
        if any(file_hash(_path(root, i['path'])) != i['sha256'] for i in plan['inputs']):
            raise PlanningEvidenceError('evidence_integrity', 'Input changed after method verification')
    validate_method_bindings(plan.get('method_bindings'), bundle, plan)
    if cfg is not None and stage in {'approve', 'execute'}:
        frozen = bundle['context']['configuration']
        if frozen != _configuration(cfg):
            raise PlanningEvidenceError('evidence_integrity', 'Method configuration changed; re-plan')
        if reprobe:
            chosen = {b['id'] for b in plan['method_bindings']}
            candidates = [r['candidate'] for r in bundle['methods'] if r['candidate']['id'] in chosen]
            work = path.parent / ('verify_' + uuid.uuid4().hex)
            work.mkdir(mode=0o700)
            current = probe_method_apis(Path(root), candidates, cfg, work)
            expected = [r['environment'] for r in bundle['methods'] if r['candidate']['id'] in chosen]
            if sorted(current, key=lambda r: r['id']) != sorted(expected, key=lambda r: r['id']):
                raise PlanningEvidenceError('api_incompatible', 'Interpreter, package version, location or API changed')
    return bundle


def verify_method_code(content, language, plan, root, *, step_id=None):
    """Prevent silent analytical API substitution and new generated functions."""
    bundle = verify_planning_evidence(root, plan)
    selected = {b['id'] for b in plan['method_bindings']}
    if step_id is not None:
        selected = set(next(s['method_ids'] for s in plan['steps'] if s['id'] == step_id))
    candidates = [r['candidate'] for r in bundle['methods'] if r['candidate']['id'] in selected and r['candidate']['language'] == language]
    if language == 'python':
        tree = ast.parse(content)
        if any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)) for n in ast.walk(tree)):
            raise PlanningEvidenceError('code_method', 'Generated custom functions/classes require separate approval')
        aliases = {}
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in {'eval', 'exec', 'compile', '__import__', 'getattr', 'setattr'}:
                raise PlanningEvidenceError('code_method', 'Dynamic API resolution is not approved')
            if isinstance(n, ast.Import):
                aliases.update({a.asname or a.name.split('.')[0]: a.name if a.asname else a.name.split('.')[0] for a in n.names})
            elif isinstance(n, ast.ImportFrom) and n.module:
                if any(a.name == '*' for a in n.names):
                    raise PlanningEvidenceError('code_method', 'Wildcard imports hide method bindings')
                aliases.update({a.asname or a.name: n.module + '.' + a.name for a in n.names})
        calls = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Call):
                value, parts = n.func, []
                while isinstance(value, ast.Attribute):
                    parts.insert(0, value.attr)
                    value = value.value
                if isinstance(value, ast.Name):
                    parts.insert(0, aliases.get(value.id, value.id))
                    calls.add('.'.join(parts))
        allowed = {api for c in candidates for api in c['apis']}
        protected = ('scipy.stats.', 'statsmodels.', 'sklearn.')
        if any(call.startswith(protected) and call not in allowed for call in calls):
            raise PlanningEvidenceError('code_method', 'Generated analytical API differs from the approved method')
    else:
        if re.search(r'\bfunction\s*\(|\\\s*\(', content):
            raise PlanningEvidenceError('code_method', 'Generated R functions require separate approval')
        allowed = {(c['package'], api) for c in candidates for api in c['apis']}
        if re.search(r'(?<![\w.:])(?:wilcox\.test|t\.test|glm|lm|aov|coxph|gsva|ssgseaParam|gsvaParam|plageParam)\s*\(', content):
            raise PlanningEvidenceError('code_method', 'Statistical R calls must use approved package namespaces')
        for pkg, api in re.findall(r'\b([A-Za-z][\w.]*):::{0,1}([A-Za-z][\w.]*)', content):
            if pkg in {'GSVA', 'stats', 'limma', 'DESeq2', 'edgeR', 'survival', 'lme4'} and (pkg, api) not in allowed:
                raise PlanningEvidenceError('code_method', 'Generated R analytical API differs from the approved method')

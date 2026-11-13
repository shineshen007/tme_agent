"""Approval-bound task environment contracts; no package installation side effects.

Source provenance is verified by the evidence collector, not by this schema.
These contracts deliberately cannot turn an unverified environment into a ready one.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from packaging.requirements import Requirement

from .task_sessions import canonical_hash

PROTOCOL = 'task_environment_v1'


def validate_spec(spec: dict) -> None:
    """Reject ambiguous package identities and mutable repository references."""
    if not isinstance(spec, dict) or spec.get('protocol') != PROTOCOL:
        raise ValueError('Unsupported task environment protocol')
    if set(spec) != {'protocol', 'interpreters', 'dependencies', 'policy'}:
        raise ValueError('Unexpected environment specification fields')
    if spec['policy'] != {'system_install': False, 'required_dependencies_only': True}:
        raise ValueError('Task environments cannot modify system software')
    interpreters = spec['interpreters']
    if not isinstance(interpreters, dict) or not interpreters or not set(interpreters) <= {'python', 'r'}:
        raise ValueError('Explicit task interpreters are required')
    for version in interpreters.values():
        if not isinstance(version, str) or not re.fullmatch(r'\d+\.\d+\.\d+', version):
            raise ValueError('Pin the existing interpreter version')
    dependencies = spec['dependencies']
    if not isinstance(dependencies, list) or not dependencies:
        raise ValueError('Task dependencies are required')
    seen = set()
    for dep in dependencies:
        if not isinstance(dep, dict) or set(dep) != {'language', 'package', 'version', 'source', 'apis'}:
            raise ValueError('Invalid dependency fields')
        language, package = dep['language'], dep['package']
        if language not in interpreters or not isinstance(package, str):
            raise ValueError('Dependency interpreter is not declared')
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]*', package):
            raise ValueError('Invalid package identity')
        identity = (language, re.sub(r'[-_.]+', '-', package).lower() if language == 'python' else package)
        if identity in seen:
            raise ValueError('Duplicate dependency identity')
        seen.add(identity)
        version = dep['version']
        if not isinstance(version, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.+_-]*', version):
            raise ValueError('An exact package version is required')
        if language == 'python':
            Requirement(package + '==' + version)
        apis = dep['apis']
        if not isinstance(apis, list) or not apis or any(
                not isinstance(api, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.]*', api)
                for api in apis) or len(set(apis)) != len(apis):
            raise ValueError('Explicit unique API names are required')
        source = dep['source']
        if not isinstance(source, dict):
            raise ValueError('Dependency source is required')
        kind = source.get('kind')
        if kind in {'pypi', 'cran', 'bioconductor'}:
            if set(source) != {'kind'} or (language == 'python') != (kind == 'pypi'):
                raise ValueError('Registry does not match dependency language')
        elif kind == 'author_repository':
            if set(source) != {'kind', 'url', 'commit', 'provenance_sha256'}:
                raise ValueError('Repository provenance binding is required')
            url = urlsplit(source['url'])
            if (url.scheme != 'https' or not url.hostname or url.username or url.password
                    or url.query or url.fragment or url.port not in {None, 443}
                    or len(url.path.strip('/').split('/')) < 2):
                raise ValueError('Repository must have a public HTTPS identity')
            if not isinstance(source['commit'], str) or not re.fullmatch(r'[0-9a-f]{40}', source['commit']):
                raise ValueError('Repository requires a full immutable commit')
            if not isinstance(source['provenance_sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', source['provenance_sha256']):
                raise ValueError('Repository requires a source evidence hash')
        else:
            raise ValueError('Unsupported dependency source')


def approved_spec(plan: dict, approved_hash: str) -> dict:
    """Return a validated spec only when the entire plan matches approval."""
    if canonical_hash(plan) != approved_hash:
        raise ValueError('Environment request differs from approved plan')
    spec = plan.get('environment_spec')
    validate_spec(spec)
    return spec

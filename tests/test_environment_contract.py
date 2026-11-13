"""Contract tests; these do not assert installation or native sandbox acceptance."""
import copy

import pytest

from multiomics_tui.environment_contract import PROTOCOL, approved_spec, validate_spec
from multiomics_tui.task_sessions import canonical_hash


@pytest.fixture
def spec():
    return {'protocol': PROTOCOL, 'interpreters': {'python': '3.14.0'},
            'policy': {'system_install': False, 'required_dependencies_only': True},
            'dependencies': [{'language': 'python', 'package': 'numpy', 'version': '2.0.0',
                              'source': {'kind': 'pypi'}, 'apis': ['numpy.mean']}]}


def test_approval_binds_dependencies(spec):
    plan = {'environment_spec': spec, 'goal': 'example'}
    approved = canonical_hash(plan)
    assert approved_spec(plan, approved) == spec
    plan['environment_spec']['dependencies'][0]['version'] = '2.1.0'
    with pytest.raises(ValueError, match='approved plan'):
        approved_spec(plan, approved)


@pytest.mark.parametrize('field,value', [('package', '--index-url'), ('version', '>=2'),
                                        ('apis', ['numpy.mean', 'numpy.mean']),
                                        ('source', {'kind': 'cran'})])
def test_invalid_dependency(spec, field, value):
    spec['dependencies'][0][field] = value
    with pytest.raises(ValueError):
        validate_spec(spec)


def test_duplicate_normalized_identity(spec):
    spec['dependencies'].append(copy.deepcopy(spec['dependencies'][0]))
    spec['dependencies'][1]['package'] = 'NumPy'
    with pytest.raises(ValueError, match='Duplicate'):
        validate_spec(spec)


@pytest.mark.parametrize('commit', ['main', 'v1.0', 'abcd123', 'g' * 40])
def test_repository_requires_fixed_commit(spec, commit):
    spec['dependencies'][0]['source'] = {'kind': 'author_repository',
        'url': 'https://github.com/example/project', 'commit': commit, 'provenance_sha256': 'a' * 64}
    with pytest.raises(ValueError, match='immutable commit'):
        validate_spec(spec)


def test_pinned_repository_contract_only(spec):
    spec['dependencies'][0]['source'] = {'kind': 'author_repository',
        'url': 'https://github.com/example/project', 'commit': 'b' * 40, 'provenance_sha256': 'a' * 64}
    validate_spec(spec)


def test_system_install_refused(spec):
    spec['policy']['system_install'] = True
    with pytest.raises(ValueError, match='system software'):
        validate_spec(spec)

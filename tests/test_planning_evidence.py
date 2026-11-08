"""Offline evidence gate tests. Source/transport fixtures are not scientific acceptance."""
import copy
import json
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from multiomics_tui import planning_evidence as pe
from multiomics_tui.task_sessions import TaskStore, canonical_hash
from multiomics_tui.task_planner import generate_plan
from planning_fixtures import DOC, PAPER, Literature, attach, binding, candidate, environment, proposal

REQUEST = 'patient continuous independent'


@pytest.fixture
def inputs(tmp_path):
    path = tmp_path / 'data/raw/input.csv'
    path.parent.mkdir(parents=True)
    path.write_text('value\n1\n2\n')
    from multiomics_tui.task_runner import file_hash
    item = {'path': 'data/raw/input.csv', 'sha256': file_hash(path)}
    audit = tmp_path / '.runtime/input_inspections/fixture.json'
    audit.parent.mkdir(parents=True)
    local = {'profiles': [{**item, 'object_class': 'data.frame', 'keys': {}, 'columns': {}}], 'apis': {}, 'software': {}}
    audit.write_text(json.dumps(local))
    local['audit_path'] = str(audit.relative_to(tmp_path))
    return local


def client(value=None):
    value = proposal() if value is None else value
    def chat(role, system, user, schema, validator):
        assert role == 'tui_method_search'
        validator(copy.deepcopy(value))
        return {'parsed': copy.deepcopy(value)}
    return SimpleNamespace(chat_json=chat, private_ids=())


def collect(root, inputs, cfg=None, **kwargs):
    return pe.collect_planning_evidence(client(), root, REQUEST, inputs, cfg or {},
        literature_service=kwargs.pop('literature_service', Literature()),
        software_transport=kwargs.pop('software_transport', lambda *a, **k: b'<html>Official docs fixture</html>'),
        probe=kwargs.pop('probe', environment), **kwargs)


def raw(inputs):
    return {'goal': REQUEST, 'analysis_unit': 'patient', 'method': 'Synthetic finite audit',
            'parameters': [], 'inputs': [inputs['profiles'][0]], 'input_bindings': {'inventory': True},
            'steps': [{'id': 'audit', 'language': 'python', 'purpose': 'Check values', 'outputs': ['tables/out.csv'],
                       'method_ids': ['finite']}], 'limitations': [], 'method_bindings': [binding()]}


def frozen(inputs, receipt):
    plan = raw(inputs)
    plan['planning_protocol'] = pe.PROTOCOL
    plan['planning_evidence'] = {k: v for k, v in receipt.items() if k != 'bundle'}
    plan['parameters'] = [{'name': 'planning_protocol', 'value': pe.PROTOCOL, 'source': 'evidence:' + receipt['sha256']}]
    return plan


@pytest.mark.parametrize('forged', [False, True])
def test_retrieval_precedes_final_planner(tmp_path, inputs, monkeypatch, forged):
    from multiomics_tui import input_inspection, r_env
    order = []
    monkeypatch.setattr(input_inspection, 'inspect_inputs', lambda *a: inputs)
    monkeypatch.setattr(r_env, 'availability_note', lambda cfg: '')
    original = pe.collect_planning_evidence
    def gather(*args, **kwargs):
        order.append('retrieval')
        return original(*args, **kwargs, literature_service=Literature(),
            software_transport=lambda *a, **k: b'official', probe=environment)
    monkeypatch.setattr(pe, 'collect_planning_evidence', gather)
    def chat(role, system, user, schema, validator):
        if role == 'tui_task_plan_inputs':
            value = {'inputs': ['data/raw/input.csv']}
        elif role == 'tui_method_search':
            order.append('query')
            value = proposal()
        else:
            order.append('plan')
            assert order[:2] == ['retrieval', 'query']
            assert 'Verified method candidates' in system
            value = raw(inputs)
            value['inputs'] = ['data/raw/input.csv']
            if forged:
                value['planning_evidence'] = {'status': 'verified'}
        validator(copy.deepcopy(value))
        return {'parsed': copy.deepcopy(value)}
    if forged:
        with pytest.raises(ValueError, match='verification receipts'):
            generate_plan(SimpleNamespace(chat_json=chat), tmp_path, REQUEST, {})
        return
    plan = generate_plan(SimpleNamespace(chat_json=chat), tmp_path, REQUEST, {})
    assert order == ['retrieval', 'query', 'plan']
    assert plan['planning_protocol'] == pe.PROTOCOL
    pe.verify_planning_evidence(tmp_path, plan, stage='approve')


def test_cache_is_version_context_and_integrity_bound(tmp_path, inputs):
    first = collect(tmp_path, inputs)
    def forbidden(*a, **k):
        pytest.fail('Fresh intact cache must not retrieve again')
    cached = collect(tmp_path, inputs, {'planning': {'evidence': {}}}, software_transport=forbidden)
    # Policy configuration is itself context-bound; use identical settings for reuse.
    assert first['sha256'] != cached['sha256']  # cache_hit is recorded in each immutable attempt
    assert cached['cache_hit']
    offline = {'planning': {'evidence': {'offline': True}}}
    assert collect(tmp_path, inputs, offline, software_transport=forbidden)['cache_hit']
    paths = list((tmp_path / '.cache/planning_evidence').rglob('*.json'))
    for path in paths:
        value = json.loads(path.read_text()); value['methods'][0]['environment']['version'] = 'tampered'
        path.write_text(json.dumps(value))
    with pytest.raises(pe.PlanningEvidenceError, match='cache'):
        collect(tmp_path, inputs, offline)


def test_expired_cache_cannot_be_used_offline(tmp_path, inputs):
    collect(tmp_path, inputs)
    path = next((tmp_path / '.cache/planning_evidence').rglob('*.json'))
    value = json.loads(path.read_text()); value.pop('sha256')
    value['retrieved_at'] = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    value['sha256'] = pe._digest(value)
    path.write_text(json.dumps(value))
    with pytest.raises(pe.PlanningEvidenceError) as error:
        collect(tmp_path, inputs, {'planning': {'evidence': {'offline': True}}})
    assert error.value.planning_failure_kind == 'evidence_expired'


def test_failed_retrieval_can_recover_without_manual_cache_deletion(tmp_path, inputs):
    with pytest.raises(pe.PlanningEvidenceError):
        collect(tmp_path, inputs, literature_service=Literature(empty=True))
    receipt = collect(tmp_path, inputs)
    pe.verify_planning_evidence(tmp_path, frozen(inputs, receipt))


@pytest.mark.parametrize('mutation', ['input', 'request', 'artifact', 'marker'])
def test_approval_rechecks_inputs_requirements_and_attempt(tmp_path, inputs, mutation):
    receipt = collect(tmp_path, inputs)
    plan = frozen(inputs, receipt)
    kwargs = {}
    if mutation == 'input':
        (tmp_path / 'data/raw/input.csv').write_text('value\n3\n4\n')
    elif mutation == 'request':
        kwargs['request'] = REQUEST + ' changed'
    elif mutation == 'artifact':
        (tmp_path / receipt['path']).with_name('context.json').write_text('{}')
    else:
        plan['parameters'] = []
    with pytest.raises(pe.PlanningEvidenceError):
        pe.verify_planning_evidence(tmp_path, plan, stage='approve', **kwargs)


@pytest.mark.parametrize('failure', ['dependency', 'empty', 'offline', 'source'])
def test_failed_required_method_never_reaches_approval(tmp_path, inputs, failure):
    kwargs, cfg = {}, {}
    if failure == 'dependency':
        kwargs['probe'] = lambda *a: [{**environment()[0], 'status': 'missing'}]
    elif failure == 'empty':
        kwargs['literature_service'] = Literature(empty=True)
    elif failure == 'source':
        kwargs['software_transport'] = lambda *a, **k: 'not bytes'
    else:
        cfg = {'planning': {'evidence': {'offline': True}}}
    with pytest.raises(pe.PlanningEvidenceError):
        collect(tmp_path, inputs, cfg, **kwargs)
    assert list((tmp_path / '.runtime/planning_evidence').rglob('failure.json'))


@pytest.mark.parametrize('mutation', ['source', 'paper', 'doc', 'method', 'step', 'pairing'])
def test_forged_evidence_and_design_conflicts_refused(tmp_path, inputs, mutation):
    receipt = collect(tmp_path, inputs)
    plan = frozen(inputs, receipt)
    if mutation == 'source':
        (tmp_path / receipt['path']).write_text('{}')
    elif mutation == 'paper':
        plan['method_bindings'][0]['paper']['source_id'] = 'PMID:9999'
    elif mutation == 'doc':
        plan['method_bindings'][0]['documentation_quote'] = 'invented documentation text'
    elif mutation == 'method':
        plan['method_bindings'][0]['id'] = 'invented'
    elif mutation == 'step':
        plan['steps'][0]['method_ids'] = ['invented']
    else:
        plan['input_bindings'] = {'matrices': [{'pair_key': None}]}
        receipt['bundle']['proposal']['design']['pairing'] = 'paired'
        with pytest.raises(pe.PlanningEvidenceError):
            pe.validate_method_bindings(plan['method_bindings'], receipt['bundle'], plan)
        return
    with pytest.raises((pe.PlanningEvidenceError, ValueError)):
        pe.verify_planning_evidence(tmp_path, plan)


def test_approval_expiry_and_frozen_execution_differ(tmp_path, inputs):
    receipt = collect(tmp_path, inputs)
    plan = frozen(inputs, receipt)
    bundle = json.loads((tmp_path / receipt['path']).read_text())
    bundle['retrieved_at'] = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    path = tmp_path / receipt['path']; path.write_text(json.dumps(bundle))
    plan['planning_evidence']['sha256'] = pe._digest(path.read_bytes())
    plan['planning_evidence']['retrieved_at'] = bundle['retrieved_at']
    plan['parameters'][0]['source'] = 'evidence:' + plan['planning_evidence']['sha256']
    with pytest.raises(pe.PlanningEvidenceError, match='expired'):
        pe.verify_planning_evidence(tmp_path, plan, stage='approve')
    pe.verify_planning_evidence(tmp_path, plan, stage='execute', reprobe=False)


def test_real_guarded_python_api_probe(project_root, stub_cfg):
    folder = project_root / 'probe'; folder.mkdir()
    rows = pe.probe_method_apis(project_root, [candidate()], stub_cfg, folder)
    assert rows[0]['status'] == 'verified'
    assert len(rows[0]['interpreter_sha256']) == 64
    assert 'x' in rows[0]['apis']['math.isfinite']['signature']


def test_windows_session_paths_are_stored_as_posix(tmp_path, inputs, monkeypatch):
    from pathlib import PureWindowsPath
    from unittest.mock import Mock
    store = Mock()
    store.directory.return_value.relative_to.return_value = PureWindowsPath('workspace/tui/sessions/task_fixture')
    store.get.return_value = {}
    monkeypatch.setattr('multiomics_tui.task_sessions.TaskStore', Mock(return_value=store))
    receipt = collect(tmp_path, inputs, task_id='task_fixture')
    assert receipt['path'].startswith('workspace/tui/sessions/task_fixture/planning/')
    assert '\\' not in receipt['path']


def test_real_guarded_r_api_probe(project_root, stub_cfg):
    folder = project_root / 'r_probe'; folder.mkdir()
    c = {'id': 'wilcox', 'name': 'Wilcoxon', 'language': 'r', 'package': 'stats',
         'apis': ['wilcox.test'], 'query': 'Wilcoxon signed rank method'}
    rows = pe.probe_method_apis(project_root, [c], stub_cfg, folder)
    assert rows[0]['status'] == 'verified', rows[0]
    from multiomics_tui.actions import rscript_path
    import shutil
    executable = rscript_path(project_root)
    assert Path(rows[0]['interpreter']).samefile(shutil.which(executable) or executable)
    assert 'paired' in rows[0]['apis']['wilcox.test']['documentation']
    assert len(rows[0]['interpreter_sha256']) == 64


def test_real_guarded_scipy_distribution_ownership(project_root, stub_cfg):
    folder = project_root / 'scipy_probe'; folder.mkdir()
    c = {'id': 'wilcox', 'name': 'Wilcoxon', 'language': 'python', 'package': 'scipy',
         'apis': ['scipy.stats.wilcoxon'], 'query': 'Wilcoxon method'}
    rows = pe.probe_method_apis(project_root, [c], stub_cfg, folder)
    assert rows[0]['status'] == 'verified'
    assert len(rows[0]['apis']['scipy.stats.wilcoxon']['module_sha256']) == 64


def test_real_guarded_gsva_exported_help(project_root, stub_cfg):
    from multiomics_tui.r_env import available_packages
    if 'GSVA' not in available_packages(stub_cfg):
        pytest.skip('GSVA installed API probe requires the optional package')
    folder = project_root / 'gsva_probe'; folder.mkdir()
    apis = ['gsva', 'plageParam', 'ssgseaParam', 'zscoreParam', 'gsvaParam']
    c = {'id': 'gsva', 'name': 'GSVA', 'language': 'r', 'package': 'GSVA', 'apis': apis, 'query': 'GSVA method'}
    rows = pe.probe_method_apis(project_root, [c], stub_cfg, folder)
    assert rows[0]['status'] == 'verified'
    assert all(len(rows[0]['apis'][api]['documentation']) > 16 for api in apis)


def test_repeated_probes_have_unique_windows_snapshot_names(tmp_path, monkeypatch):
    from multiomics_tui import task_runner, input_inspection
    names = []
    def snapshot(root, work, cfg, inputs):
        names.append(work.name)
        target = root / '.runtime/inputs' / work.name
        target.mkdir(parents=True, exist_ok=False)
        return {}
    def guarded(root, work, argv, env):
        Path(argv[-1]).write_text(json.dumps(environment()))
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    monkeypatch.setattr(task_runner, '_step_environment', snapshot)
    monkeypatch.setattr(input_inspection, '_run_guarded', guarded)
    folder = tmp_path / 'repeat_probe'; folder.mkdir()
    pe.probe_method_apis(tmp_path, [candidate()], {}, folder)
    pe.probe_method_apis(tmp_path, [candidate()], {}, folder)
    assert len(set(names)) == 2


def test_api_drift_blocks_execution(tmp_path, inputs, monkeypatch):
    plan = frozen(inputs, collect(tmp_path, inputs))
    monkeypatch.setattr(pe, 'probe_method_apis', lambda *a: [{**environment()[0], 'version': 'changed'}])
    with pytest.raises(pe.PlanningEvidenceError, match='changed'):
        pe.verify_planning_evidence(tmp_path, plan, stage='execute', cfg={})


def test_legacy_readable_but_cannot_be_newly_approved(tmp_path, inputs):
    store = TaskStore(tmp_path)
    state = store.save_plan(store.create(REQUEST)['task_id'], raw(inputs))
    assert store.plan(state['task_id'])['method']
    with pytest.raises(pe.PlanningEvidenceError, match='Legacy'):
        store.approve(state['task_id'], state['plan_hash'])
    assert store.get(state['task_id'])['approved_hash'] is None


@pytest.mark.parametrize('source', [
    'from scipy.stats import mannwhitneyu as test\ntest([1], [2])',
    'import scipy.stats as s\ns.wilcoxon([1], [2])',
    'def invented(x):\n return x\n',
    'f = lambda x: x',
])
def test_codegen_cannot_replace_approved_methods(tmp_path, inputs, source):
    plan = frozen(inputs, collect(tmp_path, inputs))
    with pytest.raises(pe.PlanningEvidenceError):
        pe.verify_method_code(source, 'python', plan, tmp_path)


def test_utility_cannot_hide_an_explicit_scientific_request():
    p = proposal(); p['kind'] = 'utility'
    with pytest.raises(pe.PlanningEvidenceError, match='bypass'):
        pe._proposal_valid(p, REQUEST + ' paired wilcoxon')


def test_privacy_blocks_queries_before_transport(tmp_path, inputs):
    p = proposal(); p['candidates'][0]['query'] = 'patient_id: PRIVATE_123 method'
    with pytest.raises(pe.PlanningEvidenceError, match='Private'):
        pe.collect_planning_evidence(client(p), tmp_path, REQUEST, inputs, {}, probe=environment)


def test_official_registry_identity_and_doc_origin_checks():
    c = {**candidate(), 'package': 'scipy', 'apis': ['scipy.stats.wilcoxon']}
    env = {**environment()[0], 'version': '1.0'}
    data = {'info': {'name': 'scipy', 'version': '1.0', 'project_urls': {'Documentation': 'https://docs.scipy.org/'}},
            'urls': [{'filename': 'fixture.whl', 'yanked': False}]}
    calls = []
    def transport(url, **kwargs):
        calls.append(url)
        return json.dumps(data).encode() if 'pypi.org' in url else b'official docs'
    assert pe.retrieve_software_sources(c, env, {}, transport)['version'] == '1.0'
    c['documentation_url'] = 'https://evil.invalid/fake'
    with pytest.raises(pe.PlanningEvidenceError, match='rooted'):
        pe.retrieve_software_sources(c, env, {}, transport)
    c.pop('documentation_url'); data['info']['name'] = 'invented'
    with pytest.raises(pe.PlanningEvidenceError, match='mismatch'):
        pe.retrieve_software_sources(c, env, {}, transport)


def test_official_python_requirement_and_bioc_catalog():
    import gzip
    c = {**candidate(), 'package': 'scipy', 'apis': ['scipy.stats.wilcoxon']}
    data = {'info': {'name': 'scipy', 'version': '1.0', 'requires_python': '<3',
                    'project_urls': {'Documentation': 'https://docs.scipy.org/'}},
            'urls': [{'filename': 'fixture.whl', 'yanked': False}]}
    with pytest.raises(pe.PlanningEvidenceError, match='Python version'):
        pe.retrieve_software_sources(c, {**environment()[0], 'version': '1.0'}, {}, lambda *a, **k: json.dumps(data).encode())
    c = {'id': 'plage', 'name': 'PLAGE', 'language': 'r', 'package': 'GSVA', 'apis': ['plageParam'], 'query': 'PLAGE method'}
    env = {**environment()[0], 'version': '1.0', 'bioconductor_package': True, 'bioconductor': '3.22'}
    def transport(url, **kwargs):
        if url.endswith('PACKAGES.gz'):
            return gzip.compress(b'Package: GSVA\nVersion: 1.0\n\n')
        return b'Official GSVA documentation Version 1.0'
    assert pe.retrieve_software_sources(c, env, {}, transport)['metadata']['official_catalog_match']['Package'] == 'GSVA'


def test_cran_catalog_identity_and_archive_lookup():
    import gzip
    c = {'id': 'plot', 'name': 'ggplot', 'language': 'r', 'package': 'ggplot2', 'apis': ['ggplot'], 'query': 'grammar graphics'}
    env = {**environment()[0], 'version': '1.0'}
    def transport(url, **kwargs):
        if url.endswith('PACKAGES.gz'):
            return gzip.compress(b'Package: ggplot2\nVersion: 2.0\n\n')
        if '/Archive/' in url:
            return b'ggplot2_1.0.tar.gz'
        return b'official package ggplot2_2.0.tar.gz'
    result = pe.retrieve_software_sources(c, env, {}, transport)
    assert result['version'] == '1.0'
    assert any('/Archive/' in row['url'] for row in result['snapshots'])


def test_utility_is_not_a_general_literature_bypass():
    p = proposal(); p['kind'] = 'utility'
    p['candidates'][0].update(package='scipy', apis=['scipy.stats.ttest_ind'])
    with pytest.raises(pe.PlanningEvidenceError, match='structural'):
        pe._proposal_valid(p, REQUEST + ' t-test')


def test_frozen_gsva_apis_must_all_be_verified(tmp_path, inputs):
    receipt = collect(tmp_path, inputs)
    plan = frozen(inputs, receipt)
    plan['pathway_protocol'] = {'param_api': {'plage': 'GSVA::plageParam'}}
    with pytest.raises(pe.PlanningEvidenceError, match='Frozen GSVA protocol'):
        pe.verify_planning_evidence(tmp_path, plan)


def test_old_validator_refuses_new_evidence_parameter(tmp_path, inputs):
    import subprocess
    import types
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(['git', 'show', 'v8.3.2:multiomics_tui/task_planner.py'], cwd=root, capture_output=True, text=True)
    if result.returncode:
        pytest.skip('Legacy validator snapshot requires the immutable v8.3.2 tag')
    legacy = types.ModuleType('multiomics_tui._legacy_planner')
    legacy.__package__ = 'multiomics_tui'
    exec(compile(result.stdout, 'legacy_task_planner.py', 'exec'), legacy.__dict__)
    receipt = collect(tmp_path, inputs)
    plan = frozen(inputs, receipt)
    plan['inputs'] = [p['path'] for p in plan['inputs']]
    with pytest.raises(ValueError, match='Numeric parameters'):
        legacy.validate_plan(tmp_path, plan, REQUEST, {})


def test_guarded_utility_acceptance(project_root, monkeypatch):
    """Actual input/API probes, approval, code preflight and guarded execution.

    Only model replies and public HTTP are fixed responses; no statistics or
    patient inputs are used. The production evidence verifier remains active.
    """
    from multiomics_tui.llm import load_tui_config
    from multiomics_tui import task_runner
    cfg = load_tui_config(project_root)
    path = project_root / 'data/raw/utility.csv'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('value\n1\n2\n', encoding='utf-8')
    original_bytes = path.read_bytes()
    request = 'Inspect CSV structure only'
    store = TaskStore(project_root)
    state = store.create(request)
    original_collect = pe.collect_planning_evidence
    def fixed_sources(*args, **kwargs):
        return original_collect(*args, **kwargs, software_transport=lambda *a, **k: b'Official R manual fixture')
    monkeypatch.setattr(pe, 'collect_planning_evidence', fixed_sources)
    class Model:
        private_ids = ()
        def chat_json(self, role, system, user, schema, validator=None):
            if role == 'tui_task_plan_inputs':
                value = {'inputs': ['data/raw/utility.csv']}
            elif role == 'tui_method_search':
                value = {'kind': 'utility', 'design': dict.fromkeys(pe.DESIGN_KEYS, 'not_applicable'),
                         'design_basis': dict.fromkeys(pe.DESIGN_KEYS, 'not_applicable'),
                         'candidates': [{'id': 'csv_io', 'name': 'CSV structural check', 'language': 'r',
                                         'package': 'utils', 'apis': ['read.csv', 'write.csv'], 'query': 'R CSV file APIs'}]}
            elif role == 'tui_task_plan':
                bundle_path = next(store.directory(state['task_id']).glob('planning/attempt_*/bundle.json'))
                bundle = json.loads(bundle_path.read_text())
                value = {'goal': request, 'analysis_unit': 'not_applicable', 'method': 'CSV structural check',
                         'parameters': [], 'inputs': ['data/raw/utility.csv'], 'input_bindings': {'inventory': True},
                         'steps': [{'id': 'inspect_csv', 'language': 'r', 'purpose': 'Check CSV structure',
                                    'outputs': ['tables/check.csv'], 'method_ids': ['csv_io']}], 'limitations': [],
                         'method_bindings': [{'id': 'csv_io', 'rationale': 'Direct mature R file APIs.',
                            'limitations': 'Synthetic structural check only.', 'alternatives': 'No inference required.',
                            'documentation_quote': bundle['methods'][0]['software']['documentation'][:100],
                            'requirements': dict.fromkeys(pe.DESIGN_KEYS, 'Not applicable to file structure.')}]}
            else:
                assert role == 'tui_task_codegen'
                script = '''d <- utils::read.csv(file.path(Sys.getenv('PROJECT_ROOT'),'data/raw/utility.csv'))
utils::write.csv(d,file.path(Sys.getenv('TASK_DIR'),'tables/check.csv'),row.names=FALSE)
'''
                value = {'files': [{'step_id': 'inspect_csv', 'content': script}], 'output_contracts': [
                    {'path': 'tables/check.csv', 'format': 'csv', 'columns': ['value'], 'structure': 'Original value column'}]}
            if validator:
                validator(value)
            return {'parsed': value}
    model = Model()
    plan = generate_plan(model, project_root, request, cfg, task_id=state['task_id'])
    state = store.save_plan(state['task_id'], plan)
    store.approve(state['task_id'], state['plan_hash'], cfg=cfg)
    assert task_runner.run(project_root, state['task_id'], client=model) == 0
    assert store.get(state['task_id'])['status'] == 'succeeded'
    assert path.read_bytes() == original_bytes
    manifest = json.loads((project_root / 'analysis' / state['task_id'] / 'result_manifest.json').read_text())
    assert manifest['final_output_revalidation']['unchanged']

"""Mixed-table input protocol regressions; synthetic data, no model transport."""
import copy
import json
import subprocess
from unittest.mock import Mock

import pytest

from multiomics_tui.input_inspection import inspect_inputs, public_inspection, validate_bindings
from multiomics_tui.task_runner import file_hash, run
from multiomics_tui.task_planner import generate_plan
from multiomics_tui.task_sessions import TaskStore, canonical_hash


@pytest.fixture
def mixed_inputs(project_root, stub_cfg, request):
    case = getattr(request, 'param', 'normal')
    folder = project_root / 'data/raw'
    folder.mkdir(parents=True)
    expression = folder / 'mixed.csv'
    text = ('feature,mz,PRIVATE_SAMPLE_B,rt,annotation,PRIVATE_SAMPLE_A\n'
            'F1,100,2,1,same,1\nF2,100,4,1,same,3\nF3,101,6,1,same,5\n')
    if case == 'nonfinite': text = text.replace('F1,100,2', 'F1,100,Inf')
    if case == 'missing_value': text = text.replace('F1,100,2', 'F1,100,NA')
    if case == 'character': text = text.replace('F1,100,2', 'F1,100,unparseable')
    if case == 'duplicate_feature': text = text.replace('F3,101', 'F1,101')
    if case == 'missing_feature': text = text.replace('F3,101', ',101')
    if case == 'ambiguous': text = text.replace('same,1', 'alt1,1').replace('same,3', 'alt2,3').replace('same,5', 'alt3,5')
    if case == 'precision': text = text.replace('F1,100,2', 'F1,100,2.0000000000000004')
    if case == 'duplicate_header': text = text.replace('annotation', 'mz')
    expression.write_text(text)
    metadata = folder / 'samples.csv'
    meta = 'sample_id,group\nPRIVATE_SAMPLE_A,Tumor\nPRIVATE_SAMPLE_B,Normal\n'
    if case == 'unmatched': meta = meta.replace('PRIVATE_SAMPLE_B', 'PRIVATE_SAMPLE_C')
    if case == 'duplicate_sample': meta = meta.replace('PRIVATE_SAMPLE_B', 'PRIVATE_SAMPLE_A')
    if case == 'missing_sample': meta = meta.replace('PRIVATE_SAMPLE_B', '')
    metadata.write_text(meta)
    if case == 'tsv':
        expression = expression.with_suffix('.tsv')
        expression.write_text(text.replace(',', '\t'))
    if case == 'rds':
        target = expression.with_suffix('.rds')
        subprocess.run([stub_cfg['runtime']['rscript'], '--vanilla', '-e',
            'a<-commandArgs(TRUE);saveRDS(utils::read.csv(a[1],check.names=FALSE),a[2])',
            str(expression), str(target)], check=True, capture_output=True)
        expression = target
    inputs = [{'path': p.relative_to(project_root).as_posix(), 'sha256': file_hash(p)}
              for p in (expression, metadata)]
    local = inspect_inputs(project_root, inputs, stub_cfg)
    return local, inputs


@pytest.mark.parametrize('mixed_inputs', ['normal', 'tsv', 'rds', 'ambiguous', 'nonfinite',
    'missing_value', 'character', 'duplicate_feature', 'missing_feature', 'unmatched',
    'duplicate_sample', 'missing_sample', 'duplicate_header'], indirect=True)
def test_mixed_view_validation(project_root, stub_cfg, mixed_inputs, request):
    local, inputs = mixed_inputs
    case = request.node.callspec.params['mixed_inputs']
    p = local['profiles'][0]
    assert not p['numeric_matrix']
    public = json.dumps(public_inspection(local))
    assert 'PRIVATE_SAMPLE' not in public and 'level_hashes' not in public
    assert all(file_hash(project_root / i['path']) == i['sha256'] for i in inputs)
    views = p['matrix_views']
    if case in {'character', 'duplicate_feature', 'missing_feature', 'unmatched',
                'duplicate_sample', 'missing_sample', 'duplicate_header'}:
        assert not views
        return
    assert len(views) == (2 if case == 'ambiguous' else 1)
    view = views[0]
    assert view['sample_positions'] == [6, 3]
    assert view['dimensions'] == [3, 2] and view['feature_ref'] == 'column_1'
    b = dict(path=inputs[0]['path'], view_id=view['view_id'], sample_axis='columns',
             metadata_path=inputs[1]['path'], sample_key='sample_id', group_key='group',
             group_map={'Tumor': 'tumor', 'Normal': 'normal'}, pair_key=None, preprocessing='none')
    if case in {'nonfinite', 'missing_value', 'ambiguous'}:
        with pytest.raises(ValueError, match='Nonfinite|Ambiguous feature'):
            validate_bindings({'matrices': [b]}, local, '')
        if case != 'ambiguous': return
    assert validate_bindings({'matrices': [b]}, local, view['view_id'])
    for field, value in [('view_id', 'view_forged'), ('sample_axis', 'rows'), ('sample_key', 'group')]:
        with pytest.raises(ValueError):
            validate_bindings({'matrices': [{**b, field: value}]}, local, view['view_id'])
    legacy = {**local, 'version': 'input_probe_v3'}
    with pytest.raises(ValueError, match='fresh input inspection'):
        validate_bindings({'matrices': [b]}, legacy, view['view_id'])


@pytest.mark.parametrize('tamper', ['none', 'input', 'approval', 'prepared'])
@pytest.mark.parametrize('mixed_inputs', ['precision'], indirect=True)
def test_approved_view_runner(project_root, stub_cfg, mixed_inputs, monkeypatch, tamper):
    local, inputs = mixed_inputs
    view = local['profiles'][0]['matrix_views'][0]
    binding = dict(path=inputs[0]['path'], view_id=view['view_id'], sample_axis='columns',
                   metadata_path=inputs[1]['path'], sample_key='sample_id', group_key='group',
                   group_map={'Tumor': 'tumor', 'Normal': 'normal'}, pair_key=None, preprocessing='none')
    raw = dict(goal='Inspect a mixed matrix view', analysis_unit='sample', method='descriptive validation',
               parameters=[], inputs=[i['path'] for i in inputs], input_bindings={'matrices': [binding]},
               steps=[dict(id='inspect_view', language='r', purpose='Verify prepared synthetic values',
                           outputs=['tables/samples.csv'])], limitations=['Synthetic engineering check'])
    client = Mock()
    client.private_ids = ()
    client.chat_json.side_effect = [{'parsed': {'inputs': raw['inputs']}}, {'parsed': raw}]
    # Keep actual sandboxed inspection; only model responses are deterministic.
    plan = generate_plan(client, project_root, 'Inspect data/raw/mixed.csv and data/raw/samples.csv', stub_cfg)
    assert client.chat_json.call_count == 2
    assert 'PRIVATE_SAMPLE' not in str(client.chat_json.call_args_list)
    changed = copy.deepcopy(plan)
    changed['input_bindings']['matrices'][0]['view_id'] = 'view_forged'
    assert canonical_hash(plan) != canonical_hash(changed)
    store = TaskStore(project_root)
    state = store.create('Inspect data/raw/mixed.csv and data/raw/samples.csv')
    state = store.save_plan(state['task_id'], plan)
    state = store.approve(state['task_id'], state['plan_hash'])
    if tamper == 'input':
        (project_root / inputs[0]['path']).write_text('changed\n')
    if tamper == 'approval':
        state['approved_hash'] = 'bad'; store.save(state)
    content = '''task <- Sys.getenv('TASK_DIR')
m <- readRDS(file.path(task,'objects/input_views/0/matrix.rds'))
stopifnot(identical(dim(m),c(3L,2L)), identical(unname(m),matrix(c(1,3,5,2.0000000000000004,4,6),3)))
csv <- as.matrix(utils::read.csv(file.path(task,'objects/input_views/0/matrix.csv'),row.names=1,check.names=FALSE))
stopifnot(identical(csv,m))
a <- utils::read.csv(file.path(task,'objects/input_views/0/annotations.csv'))
stopifnot(identical(names(a),c('feature','mz','rt','annotation')))
s <- utils::read.csv(file.path(Sys.getenv('PROJECT_ROOT'),'data/raw/samples.csv'))
s$group <- c('tumor','normal')
utils::write.csv(s,file.path(task,'tables/samples.csv'),row.names=FALSE)
'''
    if tamper == 'prepared': content += "writeLines('changed',file.path(task,'objects/input_views/0/matrix.rds'))\n"
    generated = Mock()
    generated.chat_json.return_value = {'parsed': {'files': [{'step_id': 'inspect_view', 'content': content}],
        'output_contracts': [{'path': 'tables/samples.csv', 'structure': 'sample groups', 'format': 'csv',
                             'columns': ['sample_id', 'group'], 'binding_index': 0,
                             'sample_key': 'sample_id', 'group_key': 'group'}]}}
    if tamper != 'none':
        with pytest.raises(ValueError, match='changed|Approval|trusted inspection'):
            run(project_root, state['task_id'], client=generated)
        if tamper in {'input', 'approval'}: generated.chat_json.assert_not_called()
        return
    assert run(project_root, state['task_id'], client=generated) == 0
    assert 'matrix_view_details' not in str(generated.chat_json.call_args_list)
    task = project_root / 'analysis' / state['task_id']
    manifest = json.loads((task / 'result_manifest.json').read_text())
    import pandas as pd
    frame = pd.read_csv(task / 'objects/input_views/0/matrix.csv', dtype=str, keep_default_na=False)
    frame = frame.set_index(frame.columns[0]).astype(float)
    assert frame.iloc[0, 1] == 2.0000000000000004
    assert len(manifest['prepared_views']) == 3
    assert manifest['outputs'][0]['contract_validation']['status'] == 'passed'
    assert all(file_hash(project_root / i['path']) == i['sha256'] for i in inputs)


@pytest.mark.parametrize('mixed_inputs', ['ambiguous'], indirect=True)
def test_ambiguous_feature_choice_is_local(project_root, stub_cfg, mixed_inputs):
    local, inputs = mixed_inputs
    client = Mock(); client.private_ids = ()
    client.chat_json.return_value = {'parsed': {'inputs': [i['path'] for i in inputs]}}
    result = generate_plan(client, project_root, 'Inspect data/raw/mixed.csv', stub_cfg)
    assert client.chat_json.call_count == 1
    assert len(result['questions'][0]['options']) == 2
    assert all('matrix_view=view_' in o['answer'] for o in result['questions'][0]['options'])


@pytest.mark.parametrize('view_id', ['view_ok', 'view_missing'])
def test_pathway_coverage_uses_view_feature_keys(view_id):
    from orchestrator.pathway_scoring import _trusted_gene_set_universe
    binding = {'path': 'data/raw/mixed.csv', 'view_id': view_id, 'sample_axis': 'columns'}
    local = {'profiles': [
        {'path': 'data/raw/mixed.csv', 'keys': {'rows': {'values': ['automatic_row_number']}},
         'matrix_views': [{'view_id': 'view_ok', 'keys': {'rows': {'values': ['F1', 'F2']}},
                           'constant_row_sha256': ['F2']}]},
        {'path': 'data/signature/sets.gmt', 'sha256': 'synthetic', 'gene_sets': {'sizes': [2, 1]},
         'set_name_sha256': ['setA', 'setB'], 'set_members': [['F1', 'F2'], ['absent']]}]}
    bindings = {'matrices': [binding], 'gene_sets': ['data/signature/sets.gmt']}
    if view_id == 'view_missing':
        with pytest.raises(ValueError, match='bound matrix view'):
            _trusted_gene_set_universe({'min_size': 1, 'max_size': 10}, bindings, local)
        return
    universe, resources = _trusted_gene_set_universe({'min_size': 1, 'max_size': 10}, bindings, local)
    assert universe[0]['size_in_matrix'] == 1 and universe[0]['status'] == 'used'
    assert universe[1]['status'] == 'zero_overlap' and resources[0]['sha256'] == 'synthetic'


@pytest.mark.parametrize('mixed_inputs', ['ambiguous'], indirect=True)
@pytest.mark.parametrize('language,fragment', [('en', 'Select the feature'), ('zh', '请选择')])
def test_local_matrix_questions_use_task_language(project_root, stub_cfg, mixed_inputs, monkeypatch, language, fragment):
    from types import SimpleNamespace
    from multiomics_tui import input_inspection
    local, inputs = mixed_inputs
    monkeypatch.setattr(input_inspection, 'inspect_inputs', lambda *a: local)
    def chat(role, system, request, schema, validator):
        assert role == 'tui_task_plan_inputs'
        selected = {'inputs': [i['path'] for i in inputs]}
        validator(selected)
        return {'parsed': selected}
    questions = generate_plan(SimpleNamespace(chat_json=chat), project_root,
        'Inspect data/raw/mixed.csv and data/raw/samples.csv', stub_cfg, language=language)['questions']
    assert fragment in questions[0]['prompt']
    assert ('Select the feature' if language == 'en' else '选择特征') in questions[0]['options'][0]['reason']

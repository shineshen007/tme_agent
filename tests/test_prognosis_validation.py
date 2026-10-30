"""Contract tests for the frozen prognosis FDR/not-estimable semantics.

Three layers are covered:
1. the Python side must pass the frozen ``analysis.prognosis.fdr`` into the R job and
   refuse to invent one when it is missing or invalid;
2. integration must reuse the R family/padj values (never adjust twice) and only recompute
   for legacy files without them, with ``selected = tested & padj <= fdr``;
3. the Lead-owned ``validate_prognosis_outputs`` contract: family-wise BH recomputation,
   HR/CI arithmetic, not-estimable handling, statistics.json counts and tamper rejection.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from scipy import stats as scipy_stats

ROOT = Path(__file__).resolve().parents[1]
Z975 = float(scipy_stats.norm.ppf(0.975))
FDR = 0.05


def _bh(pvalues):
    """Independent Benjamini-Hochberg implementation used only for assertions."""
    values = np.asarray(pvalues, dtype=float)
    m = len(values)
    order = np.argsort(values)
    ranked = values[order]
    scaled = ranked * m / np.arange(1, m + 1)
    monotone = np.minimum.accumulate(scaled[::-1])[::-1]
    out = np.empty(m)
    out[order] = np.clip(monotone, 0.0, 1.0)
    return out


def _family(cohort, outcome, model_type):
    return f'prognosis_{cohort}_{outcome}_{model_type}'


def _tested_row(feature, pvalue, *, cohort='triple109', outcome='os', model_type='univariable',
                coef=0.4, se=0.5, family=None, padj=np.nan, selected=False, reason=''):
    return {
        'cohort': cohort, 'outcome': outcome, 'model_type': model_type,
        'family': family if family is not None else _family(cohort, outcome, model_type),
        'feature_id': feature, 'omics': 'transcriptomics', 'entity': feature, 'axis': 'axis_demo',
        'effect_scale': 'hazard_ratio_per_sd_tumor_value',
        'n': 15, 'n_events': 8, 'coef': coef, 'se': se, 'hr': float(np.exp(coef)),
        'ci_low': float(np.exp(coef - Z975 * se)), 'ci_high': float(np.exp(coef + Z975 * se)),
        'pvalue': pvalue, 'padj': padj, 'selected': selected, 'ph_p': 0.5,
        'status': 'tested', 'reason': reason,
    }


def _not_estimable_row(feature, *, cohort='triple109', outcome='os', model_type='univariable',
                       reason='separation_or_nonconvergence', family=None):
    return {
        'cohort': cohort, 'outcome': outcome, 'model_type': model_type,
        'family': family if family is not None else _family(cohort, outcome, model_type),
        'feature_id': feature, 'omics': 'transcriptomics', 'entity': feature, 'axis': 'axis_demo',
        'effect_scale': 'hazard_ratio_per_sd_tumor_value',
        'n': 15, 'n_events': 8, 'coef': np.nan, 'se': np.nan, 'hr': np.nan,
        'ci_low': np.nan, 'ci_high': np.nan, 'pvalue': np.nan, 'padj': np.nan,
        'selected': False, 'ph_p': np.nan, 'status': 'not_estimable', 'reason': reason,
    }


def _finalize(cox, fdr=FDR):
    """Apply per-family BH + selection exactly as the R tool does."""
    cox = cox.copy()
    cox['padj'] = np.nan
    cox['selected'] = False
    tested = cox.status.eq('tested') & np.isfinite(cox.pvalue.to_numpy(dtype=float))
    for family, group in cox.groupby('family'):
        usable = group.index[tested.loc[group.index]]
        if len(usable):
            cox.loc[usable, 'padj'] = _bh(cox.loc[usable, 'pvalue'].to_numpy(dtype=float))
    cox['selected'] = cox.status.eq('tested') & cox.padj.notna() & (cox.padj <= fdr)
    return cox


def _stats(cox, fdr=FDR, **overrides):
    tested = cox.status.eq('tested')
    stats = {
        'status': 'complete',
        'fdr': fdr,
        'method': ('candidate_axis_cox_tracking_BH_within_predeclared_cohort_outcome_model_type_families'),
        'multiple_testing': ('Benjamini-Hochberg (BH) FDR within each predeclared cohort x '
                             'outcome x model_type family'),
        'n_cox_rows': len(cox),
        'n_cox_tested': int(tested.sum()),
        'n_cox_padj': int(cox.padj.notna().sum()),
        'n_cox_selected': int(cox.selected.astype(bool).sum()),
        'n_not_estimable': int((~tested).sum()),
        'families': sorted(cox.family.astype(str).unique().tolist()),
    }
    stats.update(overrides)
    return stats


def _cox_fixture():
    rows = [
        _tested_row('GENE1', 0.01),
        _tested_row('GENE2', 0.02),
        _tested_row('GENE3', 0.2),
        _tested_row('GENE4', 0.9),
        _not_estimable_row('GENE5'),
        _tested_row('GENE1', 0.01, model_type='adjusted'),
        _tested_row('GENE2', 0.5, model_type='adjusted'),
    ]
    return _finalize(pd.DataFrame(rows))


def _write_outputs(directory, cox, stats):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    cox.to_csv(directory / 'cox_models.csv', index=False)
    (directory / 'statistics.json').write_text(json.dumps(stats, indent=2), encoding='utf-8')
    pd.DataFrame({
        'patient_id': ['P01', 'P02', 'P03'],
        'os_months': [10.0, 20.0, 30.0], 'os_event': [1, 0, 1],
        'rfs_months': [8.0, 15.0, 25.0], 'rfs_event': [1, 1, 0],
        'tnm_ordinal': [1, 2, 3],
    }).to_csv(directory / 'patient_clinical.csv', index=False)
    pd.DataFrame({
        'patient_id': ['P01', 'P02', 'P03'],
        'value_type': ['axis_feature'] * 3,
        'omics': ['transcriptomics'] * 3,
        'entity': ['GENE1', 'GENE2', 'GENE3'],
        'feature_id': ['GENE1', 'GENE2', 'GENE3'],
        'panel': [''] * 3,
        'value': [1.0, 2.0, 3.0],
    }).to_csv(directory / 'patient_features.csv', index=False)
    return directory


@pytest.fixture
def fixture(tmp_path):
    cox = _cox_fixture()
    stats = _stats(cox)
    return _write_outputs(tmp_path / 'prognosis', cox, stats), cox, stats


def _validate(directory, stats):
    from orchestrator.checkers import validate_prognosis_outputs
    return validate_prognosis_outputs(directory, stats)


# --------------------------------------------------------------------------------------
# Lead-owned validator contract
# --------------------------------------------------------------------------------------
def test_valid_prognosis_outputs_pass(fixture):
    directory, _, stats = fixture
    summary = _validate(directory, stats)
    assert summary['n_cox_rows'] == 7
    assert summary['n_tested'] == 6


def test_within_family_padj_is_recomputed(fixture, tmp_path):
    directory, cox, stats = fixture
    tampered = cox.copy()
    index = tampered.index[tampered.feature_id.eq('GENE1')][0]
    tampered.loc[index, 'padj'] = 0.9
    _write_outputs(tmp_path / 'tampered', tampered, stats)
    with pytest.raises(ValueError, match='BH-adjusted'):
        _validate(tmp_path / 'tampered', stats)


def test_global_bh_adjustment_is_rejected(fixture, tmp_path):
    directory, cox, stats = fixture
    tampered = cox.copy()
    tested = tampered.status.eq('tested')
    tampered.loc[tested, 'padj'] = _bh(tampered.loc[tested, 'pvalue'].to_numpy(dtype=float))
    _write_outputs(tmp_path / 'global', tampered, stats)
    with pytest.raises(ValueError, match='BH-adjusted'):
        _validate(tmp_path / 'global', stats)


def test_selected_must_match_tested_and_fdr(fixture, tmp_path):
    directory, cox, stats = fixture
    tampered = cox.copy()
    index = tampered.index[tampered.feature_id.eq('GENE2')][0]
    assert tampered.loc[index, 'padj'] <= FDR
    tampered.loc[index, 'selected'] = False
    _write_outputs(tmp_path / 'selected', tampered, stats)
    with pytest.raises(ValueError, match='selected is inconsistent'):
        _validate(tmp_path / 'selected', stats)


def test_not_estimable_row_cannot_be_selected(fixture, tmp_path):
    directory, cox, stats = fixture
    tampered = cox.copy()
    index = tampered.index[tampered.status.eq('not_estimable')][0]
    tampered.loc[index, 'selected'] = True
    _write_outputs(tmp_path / 'ne-selected', tampered, stats)
    with pytest.raises(ValueError, match='cannot be selected'):
        _validate(tmp_path / 'ne-selected', stats)


def test_family_label_must_match_columns(fixture, tmp_path):
    directory, cox, stats = fixture
    tampered = cox.copy()
    tampered.loc[tampered.index[0], 'family'] = 'prognosis_not_a_family'
    tampered_stats = _stats(tampered)
    _write_outputs(tmp_path / 'family', tampered, tampered_stats)
    with pytest.raises(ValueError, match='family does not match'):
        _validate(tmp_path / 'family', tampered_stats)


def test_missing_family_or_padj_column_is_rejected(fixture, tmp_path):
    directory, cox, stats = fixture
    tampered = cox.drop(columns=['padj'])
    _write_outputs(tmp_path / 'legacy', tampered, stats)
    with pytest.raises(ValueError, match='missing column: padj'):
        _validate(tmp_path / 'legacy', stats)


@pytest.mark.parametrize('column,value,match', [
    ('ci_high', np.inf, 'missing or nonfinite'),
    ('hr', np.nan, 'missing or nonfinite'),
    ('pvalue', 1.2, 'Invalid Cox p-value'),
    ('hr', 1.01, 'not exp'),
    ('ci_low', 0.01, 'confidence bound'),
    ('n_events', 20, 'Invalid model sample'),
])
def test_tampered_numeric_contract_is_rejected(fixture, tmp_path, column, value, match):
    directory, cox, stats = fixture
    tampered = cox.copy()
    index = tampered.index[tampered.status.eq('tested')][0]
    tampered.loc[index, column] = value
    _write_outputs(tmp_path / 'numeric', tampered, stats)
    with pytest.raises(ValueError, match=match):
        _validate(tmp_path / 'numeric', stats)


def test_not_estimable_row_must_keep_na_estimates_and_reason(fixture, tmp_path):
    directory, cox, stats = fixture
    bad_estimate = cox.copy()
    index = bad_estimate.index[bad_estimate.status.eq('not_estimable')][0]
    bad_estimate.loc[index, 'coef'] = 0.5
    _write_outputs(tmp_path / 'na', bad_estimate, stats)
    with pytest.raises(ValueError, match='NA estimates'):
        _validate(tmp_path / 'na', stats)

    bad_reason = cox.copy()
    bad_reason.loc[index, 'reason'] = 'looks_fine'
    _write_outputs(tmp_path / 'reason', bad_reason, stats)
    with pytest.raises(ValueError, match='recognized reason'):
        _validate(tmp_path / 'reason', stats)


def test_duplicate_model_rows_are_rejected(fixture, tmp_path):
    directory, cox, stats = fixture
    tampered = pd.concat([cox, cox.iloc[[0]]], ignore_index=True)
    duplicates_stats = _stats(tampered)
    _write_outputs(tmp_path / 'dup', tampered, duplicates_stats)
    with pytest.raises(ValueError, match='Duplicate Cox model'):
        _validate(tmp_path / 'dup', duplicates_stats)


def test_selected_must_be_boolean(fixture, tmp_path):
    directory, cox, stats = fixture
    tampered = cox.copy()
    tampered['selected'] = tampered['selected'].astype(object)
    tampered.loc[tampered.index[0], 'selected'] = 2
    _write_outputs(tmp_path / 'bool', tampered, stats)
    with pytest.raises(ValueError, match='explicit boolean'):
        _validate(tmp_path / 'bool', stats)


@pytest.mark.parametrize('overrides,match', [
    ({'n_cox_selected': 99}, 'n_cox_selected'),
    ({'n_cox_padj': 1}, 'n_cox_padj'),
    ({'n_not_estimable': 0}, 'n_not_estimable'),
    ({'families': ['prognosis_fake']}, 'families disagree'),
    ({'fdr': 2.0}, 'between zero and one'),
    ({'method': 'coxph only', 'multiple_testing': 'none'}, 'must state BH'),
])
def test_statistics_counts_and_declaration_are_checked(fixture, tmp_path, overrides, match):
    directory, cox, stats = fixture
    tampered_stats = _stats(cox)
    tampered_stats.update(overrides)
    _write_outputs(tmp_path / 'stats', cox, tampered_stats)
    with pytest.raises(ValueError, match=match):
        _validate(tmp_path / 'stats', tampered_stats)


def test_statistics_requires_frozen_fdr(fixture, tmp_path):
    directory, cox, stats = fixture
    missing = dict(stats)
    missing.pop('fdr')
    _write_outputs(tmp_path / 'no-fdr', cox, missing)
    with pytest.raises(ValueError, match='frozen fdr'):
        _validate(tmp_path / 'no-fdr', missing)


# --------------------------------------------------------------------------------------
# Frozen FDR is handed to the R job, never guessed
# --------------------------------------------------------------------------------------
def test_frozen_fdr_requires_explicit_valid_config():
    from orchestrator.prognosis import _frozen_fdr
    assert _frozen_fdr({'fdr': 0.05}) == 0.05
    assert _frozen_fdr({'fdr': '0.01'}) == 0.01
    for bad in ({}, {'fdr': None}, {'fdr': 0}, {'fdr': 1}, {'fdr': 1.5},
                {'fdr': -0.1}, {'fdr': float('nan')}, {'fdr': float('inf')}, {'fdr': 'abc'}):
        with pytest.raises(ValueError, match='fdr'):
            _frozen_fdr(bad)


def _minimal_prognosis_project(tmp_path):
    root = tmp_path / 'project'
    run = tmp_path / 'run'
    (root / 'knowledge/resources').mkdir(parents=True)
    (root / 'schemas').mkdir(parents=True)
    shutil.copy(ROOT / 'schemas/prognosis_output.schema.json',
                root / 'schemas/prognosis_output.schema.json')
    import yaml
    (root / 'knowledge/tme_metabolism_axes.yaml').write_text(
        yaml.safe_dump({'axes': [{'axis': 'axis_demo', 'genes': ['GENE1'], 'compounds': []}]}),
        encoding='utf-8')
    (root / 'knowledge/resources/tcell_signatures.gmt').write_text('', encoding='utf-8')
    (run / 'inputs').mkdir(parents=True)
    (run / 'pathways/transcriptomics/triple109').mkdir(parents=True)
    pd.DataFrame({'feature_id': ['ENSG1'], 'symbol': ['GENE1'],
                  'selected_representative': [True]}).to_csv(
        run / 'pathways/transcriptomics/triple109/gene_mapping_audit.csv', index=False)
    pd.DataFrame({'compound_id': [], 'feature_id': []}).to_csv(
        run / 'inputs/metabolite_annotations.csv', index=False)
    for omics in ('transcriptomics', 'proteomics', 'metabolomics'):
        (run / omics / 'triple109').mkdir(parents=True)
        pd.DataFrame({'feature_id': ['x']}).to_csv(run / omics / 'triple109/expression.csv', index=False)
    cfg = {
        'analysis': {'prognosis': {'axis': 'axis_demo', 'fdr': 0.05, 'min_events': 1},
                     'tcell': {'panels': [], 'deconvolution_celltypes': [], 'signature_min_size': 2}},
        'study': {'seed': 2024},
        'runtime': {'rscript': 'Rscript', 'r_library_paths': [], 'retries': 1},
    }
    return root, run, cfg


def test_analyze_prognosis_passes_frozen_fdr_into_r_job(tmp_path, monkeypatch):
    from orchestrator import prognosis as prognosis_module
    root, run, cfg = _minimal_prognosis_project(tmp_path)
    captured = {}

    def fake_invoke(root_, run_, cfg_, job, script):
        captured.update(job)
        raise RuntimeError('job captured before R execution')

    monkeypatch.setattr(prognosis_module, 'invoke_r', fake_invoke)
    result = prognosis_module.analyze_prognosis(root, run, cfg)
    assert captured['fdr'] == 0.05
    assert captured['min_events'] == 1
    assert captured['omics'] == 'prognosis_candidate_axis'
    assert result['status'] == 'failed' and result['errors']
    assert 'job captured before R execution' in result['errors'][0]['message']


def test_analyze_prognosis_fails_clearly_without_fdr(tmp_path, monkeypatch):
    from orchestrator import prognosis as prognosis_module
    root, run, cfg = _minimal_prognosis_project(tmp_path)
    calls = []
    monkeypatch.setattr(prognosis_module, 'invoke_r', lambda *args, **kwargs: calls.append(args))
    cfg['analysis']['prognosis'].pop('fdr')
    with pytest.raises(ValueError, match='fdr'):
        prognosis_module.analyze_prognosis(root, run, cfg)
    assert calls == [], 'R job must not run without a frozen FDR'


def test_analyze_prognosis_rejects_invalid_fdr(tmp_path, monkeypatch):
    from orchestrator import prognosis as prognosis_module
    root, run, cfg = _minimal_prognosis_project(tmp_path)
    monkeypatch.setattr(prognosis_module, 'invoke_r', lambda *args, **kwargs: None)
    cfg['analysis']['prognosis']['fdr'] = 1.5
    with pytest.raises(ValueError, match='fdr'):
        prognosis_module.analyze_prognosis(root, run, cfg)


# --------------------------------------------------------------------------------------
# Integration reuses R family/padj; fallback only for legacy outputs
# --------------------------------------------------------------------------------------
AXES = [{'axis': 'axis_demo', 'genes': ['GENE1'], 'compounds': [], 'origin': 'existing_candidate'}]


def _integration_cfg(fdr=FDR):
    return {
        'analysis': {'prognosis': {'axis': 'axis_demo', 'fdr': fdr},
                     'tcell': {'deconvolution_celltypes': [], 'fdr': 0.1,
                               'purity_sensitivity': False}},
        'study': {'seed': 2024},
    }


def _integration_run(tmp_path, cox, *, drop_legacy=False):
    run = tmp_path / 'run'
    (run / 'prognosis').mkdir(parents=True)
    (run / 'integration').mkdir(parents=True)
    if drop_legacy:
        cox = cox.drop(columns=['family', 'padj', 'selected'])
    cox.to_csv(run / 'prognosis/cox_models.csv', index=False)
    return run


def _integration_cox():
    rows = [
        _tested_row('GENE1', 0.01),
        _tested_row('GENE2', 0.02),
        _tested_row('GENE3', 0.2),
        _tested_row('GENE4', 0.9),
        _not_estimable_row('GENE5'),
        _tested_row('GENE1', 0.01, model_type='adjusted'),
        _tested_row('GENE2', 0.5, model_type='adjusted'),
    ]
    cox = _finalize(pd.DataFrame(rows))
    # The integration layer must recompute selection from the frozen fdr, not trust R's flag.
    cox['selected'] = False
    return cox


def test_candidate_axis_evidence_reuses_family_wise_padj(tmp_path):
    from orchestrator.integration import candidate_axis_evidence
    cox = _integration_cox()
    run = _integration_run(tmp_path, cox)
    prog_rows, tcell_rows = candidate_axis_evidence(run, _integration_cfg(), AXES)
    output = pd.read_csv(run / 'integration/prognosis_association.csv')

    assert len(tcell_rows) == 0
    assert output.family.tolist() == cox.family.tolist()
    assert np.allclose(output.padj, cox.padj, atol=1e-12, equal_nan=True)
    assert output.selected.equals(output.status.eq('tested') & output.padj.notna() & (output.padj <= FDR))
    assert output.loc[output.status.eq('not_estimable'), 'selected'].eq(False).all()
    assert output.loc[output.status.eq('not_estimable'), 'padj'].isna().all()
    assert output.cohort_id.eq(output.cohort).all()
    assert output.evidence_layer.eq('prognosis').all()
    assert output.claim_type.eq('prognostic_association').all()
    assert output.evidence_id.is_unique
    # A global adjustment would have produced different values; assert the fixture is diagnostic.
    tested = cox.status.eq('tested')
    global_padj = _bh(cox.loc[tested, 'pvalue'].to_numpy(dtype=float))
    assert not np.allclose(global_padj, cox.loc[tested, 'padj'].to_numpy(dtype=float), atol=1e-8)
    assert len(prog_rows) == len(cox)


def test_candidate_axis_evidence_falls_back_for_legacy_files(tmp_path):
    from orchestrator.integration import candidate_axis_evidence
    cox = _integration_cox()
    run = _integration_run(tmp_path, cox, drop_legacy=True)
    candidate_axis_evidence(run, _integration_cfg(), AXES)
    output = pd.read_csv(run / 'integration/prognosis_association.csv')

    expected_family = [_family(c, o, m) for c, o, m in zip(cox.cohort, cox.outcome, cox.model_type)]
    assert output.family.astype(str).tolist() == expected_family
    expected = cox.padj.to_numpy(dtype=float)
    tested = cox.status.eq('tested')
    assert np.allclose(output.padj, expected, atol=1e-8, equal_nan=True)
    assert output.selected.equals(tested & output.padj.notna() & (output.padj <= FDR))


def test_candidate_axis_evidence_rejects_missing_padj_for_tested(tmp_path):
    from orchestrator.integration import candidate_axis_evidence
    cox = _integration_cox()
    cox.loc[cox.status.eq('tested').idxmax(), 'padj'] = np.nan
    run = _integration_run(tmp_path, cox)
    with pytest.raises(ValueError, match='finite family-wise padj'):
        candidate_axis_evidence(run, _integration_cfg(), AXES)


# --------------------------------------------------------------------------------------
# Report text must expose family/padj/not-estimable wording (and not crash without stats)
# --------------------------------------------------------------------------------------
def _report_fixture(tmp_path, with_prognosis=True):
    from orchestrator.report import render_report
    run = tmp_path / 'run'
    for folder in ('validation', 'report', 'prognosis'):
        (run / folder).mkdir(parents=True, exist_ok=True)
    (run / 'validation/validate_report.json').write_text(json.dumps({
        'cohorts': {'metabolomics': {'n_qc': 2}}, 'clinical_conflicts_resolved': 0}), encoding='utf-8')
    (run / 'report/hypotheses.json').write_text(json.dumps({'hypotheses': [], 'limitations': []}))
    if with_prognosis:
        (run / 'prognosis/statistics.json').write_text(json.dumps({
            'status': 'complete', 'fdr': 0.05,
            'n_events_os_triple109': 9, 'n_events_rfs_triple109': 10,
            'n_cox_rows': 20, 'n_cox_tested': 16, 'n_cox_padj': 16,
            'n_cox_selected': 1, 'n_not_estimable': 4,
            'n_patients_same_batch108': 15,
            'families': ['prognosis_triple109_os_univariable'], 'signature_status': 'blocked',
        }), encoding='utf-8')
    state = {}
    for key, omics in (('rna_result', 'transcriptomics'),
                       ('protein_result', 'proteomics'),
                       ('metabolite_result', 'metabolomics')):
        state[key] = {'omics': omics, 'status': 'review_only', 'errors': [], 'de': [{
            'cohort_id': 'triple109', 'n_pairs': 10, 'n_features': 5,
            'n_tested': 4, 'up': 1, 'down': 1}]}
    state['prognosis_result'] = {'status': 'review_only', 'axis': 'catecholamine_3mt'} if with_prognosis else None
    state['pathway_status'] = {}
    state['integration_status'] = {}
    return render_report(run, state), run


def test_report_mentions_family_wise_padj_and_not_estimable(tmp_path):
    status, run = _report_fixture(tmp_path)
    summary = (run / 'report/summary.md').read_text(encoding='utf-8')
    assert 'Benjamini-Hochberg' in summary
    assert 'not-estimable' in summary
    assert 'padj' in summary
    assert 'fdr = 0.05' in summary
    assert 'prognosis_triple109_os_univariable' in summary
    assert '不重复校正' in summary
    assert status['prognosis_layer'] == 'review_only'
    # The absent scRNA layer degrades the run; the prognosis section itself is complete.
    assert status['technical_status'] in {'complete', 'partial'}


def test_report_without_prognosis_statistics_does_not_crash(tmp_path):
    status, run = _report_fixture(tmp_path, with_prognosis=False)
    summary = (run / 'report/summary.md').read_text(encoding='utf-8')
    assert '预注册候选轴追踪' not in summary
    assert status['prognosis_layer'] == 'absent'

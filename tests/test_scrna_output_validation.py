"""Contract tests for ``orchestrator.checkers.validate_scrna_outputs`` (method version 2).

The fixture is a small but complete v2 scRNA output tree. Each test mutates exactly one
scientific/numeric property and expects a ValueError, so a validator that silently accepts a
tampered result fails here. Metadata columns (claim_type/method/reference_panel/...) must be
excluded when identifying cell types in the proportion tables: the fixture carries text values
in those columns, which the legacy validator used to feed into a float cast.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from scipy.stats import false_discovery_control

from orchestrator.checkers import validate_scrna_outputs

CELLTYPES = ['CD8T cell', 'Malignant cell']
GENES = ['GENE1', 'GENE2', 'GENE3']
PANELS = ['pancancer', 'hcc_reference', 'pancancer_top50']
METADATA = {'biospecimen_id', 'patient_id', 'tissue', 'panel', 'claim_type', 'method',
            'reference_panel'}


def _bh(pvalues):
    values = np.asarray(pvalues, dtype=float)
    adjusted = np.full(values.shape, np.nan)
    usable = np.isfinite(values)
    adjusted[usable] = false_discovery_control(values[usable], method='bh')
    return adjusted


def _write(path, frame):
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False)


def _read(path):
    return pd.read_csv(path)


def _rewrite(path, mutate):
    frame = pd.read_csv(path)
    mutate(frame)
    frame.to_csv(path, index=False)


def _rewrite_json(path, mutate):
    payload = json.loads(path.read_text(encoding='utf-8'))
    mutate(payload)
    path.write_text(json.dumps(payload), encoding='utf-8')


def build_valid_outputs(tmp_path):
    """Complete, internally consistent v2 output tree used as the positive control."""
    scrna = tmp_path/'scrna'
    deconv = scrna/'deconvolution'
    deconv.mkdir(parents=True)

    analyses = [{'name': name, 'status': 'complete', 'method': f'{name}_test_method'}
                for name in ['inventory', 'axis_localization', 'deconvolution', 'reference_audit',
                             'signature_matrix_audit', 'reference_sensitivity']]
    analyses.append({'name': 'geneset_celltype', 'status': 'blocked',
                     'note': 'blocked_no_frozen_bulk_rna_de'})
    stats = {
        'status': 'complete',
        'method_version': 2,
        'pseudobulk_units': {'mean_cpm': 'rowMeans(pb_cpm[genes, kept groups])',
                             'mean_logcpm': 'rowMeans(log1p(pb_cpm[genes, kept groups]))',
                             'n_pseudobulk_samples': 'kept sample x celltype groups'},
        'marker_methods': {'primary': 'presto_cell_level_ranked',
                           'sensitivity': 'donor_pseudobulk_limma_sensitivity_only'},
        'reference_methods': {'signature_matrix': 'union_markers_no_zero_fill',
                              'signature_scale': 'cpm'},
        'estimate_boundary': 'NNLS proportions are estimates, not measured cell fractions',
        'analyses': analyses,
    }
    (scrna/'statistics.json').write_text(json.dumps(stats, indent=2), encoding='utf-8')

    # axis localization: realistic mean CPM and mean log1p-CPM derived from the same group values.
    group_values = {
        ('GENE1', 'CD8T cell'): [120.0, 90.0, 60.0],
        ('GENE2', 'CD8T cell'): [40.0, 30.0, 20.0],
        ('GENE3', 'CD8T cell'): [5.0, 3.0, 1.0],
        ('GENE1', 'Malignant cell'): [10.0, 12.0, 8.0],
        ('GENE2', 'Malignant cell'): [200.0, 180.0, 150.0],
        ('GENE3', 'Malignant cell'): [30.0, 25.0, 20.0],
    }
    loc_rows = []
    donor_p = [0.01, 0.20, 0.04, 0.30, 0.02, 0.90]
    cell_p = [0.001, 0.50, 0.020, 0.60, 0.005, 0.80]
    for index, ((gene, celltype), values) in enumerate(group_values.items()):
        loc_rows.append({
            'gene': gene, 'celltype': celltype, 'status': 'tested',
            'claim_type': 'external_context', 'cohort_id': 'external_atlas',
            'family_donor': 'axis_localization_donor_paired',
            'donor_pvalue': donor_p[index], 'donor_padj': np.nan,
            'pct_cells_expressing': 42.0 + index, 'mean_expr_lognorm': 1.5 + 0.1*index,
            'pseudobulk_mean_cpm': float(np.mean(values)),
            'pseudobulk_mean_logcpm': float(np.mean(np.log1p(values))),
            'n_pseudobulk_samples': 3,
            'family_cell': 'axis_localization_cell_level',
            'cell_level_pvalue': cell_p[index], 'cell_level_padj': np.nan,
        })
    loc = pd.DataFrame(loc_rows)
    loc['donor_padj'] = _bh(loc['donor_pvalue'])
    loc['cell_level_padj'] = _bh(loc['cell_level_pvalue'])
    _write(scrna/'axis_localization.csv', loc)

    # deconvolution paired estimates, BH computed within each panel family.
    paired_p = {'pancancer': [0.01, 0.40], 'hcc_reference': [0.03, 0.20],
                'pancancer_top50': [0.02, 0.60]}
    paired_rows = []
    for panel, pvalues in paired_p.items():
        for celltype, pvalue in zip(CELLTYPES, pvalues):
            paired_rows.append({
                'panel': panel, 'celltype': celltype, 'effect': 0.12, 'se': 0.04,
                'statistic': 2.5, 'pvalue': pvalue, 'ci_low': 0.03, 'ci_high': 0.21,
                'n_pairs': 30, 'df': 29.0, 'contrast': 'tumor_vs_adjacent',
                'method': f'nnls_deconvolution_paired_limma_{panel}',
                'reference_panel': panel, 'panel_role': 'primary' if panel == 'pancancer' else 'sensitivity',
                'marker_method': 'presto_cell_level_ranked_top_n_primary',
                'claim_type': 'estimate_not_measured_fraction',
                'family': f'deconvolution_paired_{panel}',
            })
    paired = pd.DataFrame(paired_rows)
    paired['padj'] = np.nan
    for panel in PANELS:
        mask = paired['panel'] == panel
        paired.loc[mask, 'padj'] = _bh(paired.loc[mask, 'pvalue'])
    _write(deconv/'deconvolution_paired.csv', paired)

    # estimated proportions: metadata columns carry text and must never be treated as cell types.
    props = pd.DataFrame({
        'biospecimen_id': ['B1', 'B1', 'B2', 'B2'],
        'patient_id': ['P1', 'P1', 'P2', 'P2'],
        'tissue': ['tumor', 'adjacent', 'tumor', 'adjacent'],
        'panel': ['pancancer']*4,
        'CD8T cell': [0.30, 0.20, 0.40, 0.25],
        'Malignant cell': [0.70, 0.80, 0.60, 0.75],
        'claim_type': ['estimate_not_measured_fraction']*4,
        'method': ['nnls_nonnegative_least_squares_pancancer_signature']*4,
        'reference_panel': ['pancancer']*4,
    })
    for panel in ('pancancer', 'hcc_reference'):
        frame = props.copy()
        frame['panel'] = panel
        frame['reference_panel'] = panel
        frame['method'] = f'nnls_nonnegative_least_squares_{panel}_signature'
        _write(deconv/f'cell_proportions_{panel}.csv', frame)

    for panel in PANELS:
        genes = [g for g in GENES]
        signature = pd.DataFrame({
            'gene': genes,
            'CD8T cell': [120.0, 40.0, 5.0],
            'Malignant cell': [10.0, 200.0, 30.0],
        })
        _write(deconv/f'signature_matrix_{panel}.csv', signature)

    audit_rows = []
    for panel in PANELS:
        for celltype in CELLTYPES:
            audit_rows.append({
                'panel': panel, 'reference_panel': panel, 'panel_role':
                    'primary' if panel == 'pancancer' else 'sensitivity_matched_source',
                'celltype': celltype, 'n_samples': 32, 'n_groups_kept': 34,
                'n_cells': 4200, 'n_sources': 2, 'source_composition': 'normal:12|tumor:22',
                'n_markers': 50, 'marker_method': 'presto_cell_level_ranked_top_n_primary',
                'source_restriction': 'pan_cancer' if panel == 'pancancer' else 'tumor',
                'confounding_flags': 'none',
            })
    _write(deconv/'reference_audit.csv', pd.DataFrame(audit_rows))

    _write(deconv/'signature_matrix_audit.csv', pd.DataFrame({
        'panel': PANELS,
        'reference_panel': PANELS,
        'marker_method': ['presto_cell_level_ranked_top_n_primary']*3,
        'panel_role': ['primary', 'sensitivity_matched_source', 'sensitivity_marker_depth'],
        'n_genes': [150, 150, 450],
        'n_markers': [150, 150, 450],
        'n_marker_entries': [150, 150, 450],
        'n_celltypes': [2, 2, 2],
        'scale': ['cpm']*3,
        'n_entries': [300, 300, 900],
        'n_zero_entries': [0, 5, 10],
        'zero_fraction': [0.0, 5/300, 10/900],
        'n_forced_zero_entries': [0, 0, 0],
        'max_abs_deviation_from_reference_mean': [0.0]*3,
        'median_abs_deviation_from_reference_mean': [0.0]*3,
        'source_restriction': ['pan_cancer', 'tumor', 'pan_cancer'],
    }))

    sensitivity_rows = []
    for comparison, panel_b in [('pancancer_vs_hcc_reference', 'hcc_reference'),
                                ('pancancer_vs_pancancer_top50', 'pancancer_top50')]:
        for celltype in CELLTYPES:
            sensitivity_rows.append({
                'comparison': comparison, 'panel_a': 'pancancer', 'panel_b': panel_b,
                'celltype': celltype, 'n': 30, 'spearman_rho': 0.82,
                'mean_abs_difference': 0.03, 'direction_concordance': 0.90,
                'n_direction_pairs': 30,
                'claim_type': 'descriptive_reference_comparison',
                'method': 'paired_patient_proportion_comparison_across_reference_panels',
                'note': 'descriptive_reference_sensitivity_not_inferential',
            })
    _write(deconv/'reference_sensitivity.csv', pd.DataFrame(sensitivity_rows))
    return scrna


# --------------------------------------------------------------------------------------
# Positive control
# --------------------------------------------------------------------------------------
def test_valid_v2_outputs_pass(tmp_path):
    scrna = build_valid_outputs(tmp_path)
    summary = validate_scrna_outputs(scrna, json.loads((scrna/'statistics.json').read_text()))
    assert summary['n_localization_rows'] == 6
    assert summary['n_paired_rows'] == 6
    assert summary['n_proportion_tables'] == 2


def test_metadata_columns_are_not_treated_as_cell_types(tmp_path):
    """claim_type/method/reference_panel hold strings; only the real cell types are proportions."""
    scrna = build_valid_outputs(tmp_path)
    props = _read(scrna/'deconvolution/cell_proportions_pancancer.csv')
    celltype_columns = [c for c in props.columns if c not in METADATA]
    assert celltype_columns == CELLTYPES
    assert props[celltype_columns].sum(axis=1).round(6).eq(1).all()
    validate_scrna_outputs(scrna, json.loads((scrna/'statistics.json').read_text()))


def test_optional_limma_sensitivity_matrix_is_validated_when_present(tmp_path):
    scrna = build_valid_outputs(tmp_path)
    deconv = scrna/'deconvolution'
    good = pd.DataFrame({'gene': GENES, 'CD8T cell': [1.0, 2.0, 3.0],
                         'Malignant cell': [4.0, 5.0, 6.0]})
    _write(deconv/'signature_matrix_pancancer_limma_sensitivity.csv', good)
    validate_scrna_outputs(scrna, json.loads((scrna/'statistics.json').read_text()))
    bad = good.copy()
    bad.loc[0, 'CD8T cell'] = -1.0
    _write(deconv/'signature_matrix_pancancer_limma_sensitivity.csv', bad)
    with pytest.raises(ValueError):
        validate_scrna_outputs(scrna, json.loads((scrna/'statistics.json').read_text()))


# --------------------------------------------------------------------------------------
# statistics.json contract
# --------------------------------------------------------------------------------------
def test_rejects_legacy_method_version(tmp_path):
    scrna = build_valid_outputs(tmp_path)
    _rewrite_json(scrna/'statistics.json', lambda s: s.__setitem__('method_version', 1))
    with pytest.raises(ValueError):
        validate_scrna_outputs(scrna, json.loads((scrna/'statistics.json').read_text()))


def test_rejects_missing_pseudobulk_units(tmp_path):
    scrna = build_valid_outputs(tmp_path)
    _rewrite_json(scrna/'statistics.json', lambda s: s.pop('pseudobulk_units'))
    with pytest.raises(ValueError):
        validate_scrna_outputs(scrna, json.loads((scrna/'statistics.json').read_text()))


def test_rejects_missing_estimate_boundary(tmp_path):
    scrna = build_valid_outputs(tmp_path)
    _rewrite_json(scrna/'statistics.json', lambda s: s.pop('estimate_boundary'))
    with pytest.raises(ValueError):
        validate_scrna_outputs(scrna, json.loads((scrna/'statistics.json').read_text()))


def test_rejects_incomplete_audit_analysis(tmp_path):
    scrna = build_valid_outputs(tmp_path)

    def drop_reference_audit(stats):
        stats['analyses'] = [a for a in stats['analyses'] if a['name'] != 'reference_audit']
    _rewrite_json(scrna/'statistics.json', drop_reference_audit)
    with pytest.raises(ValueError):
        validate_scrna_outputs(scrna, json.loads((scrna/'statistics.json').read_text()))


# --------------------------------------------------------------------------------------
# axis_localization: pseudobulk units and BH families
# --------------------------------------------------------------------------------------
def test_rejects_jensen_violation_in_pseudobulk_units(tmp_path):
    scrna = build_valid_outputs(tmp_path)

    def break_jensen(frame):
        index = frame.index[0]
        frame.loc[index, 'pseudobulk_mean_logcpm'] = float(
            np.log1p(frame.loc[index, 'pseudobulk_mean_cpm'])) + 1.0
    _rewrite(scrna/'axis_localization.csv', break_jensen)
    with pytest.raises(ValueError):
        validate_scrna_outputs(scrna, json.loads((scrna/'statistics.json').read_text()))


def test_rejects_negative_pseudobulk_mean_cpm(tmp_path):
    scrna = build_valid_outputs(tmp_path)
    _rewrite(scrna/'axis_localization.csv',
             lambda frame: frame.__setitem__('pseudobulk_mean_cpm', -5.0))
    with pytest.raises(ValueError):
        validate_scrna_outputs(scrna, json.loads((scrna/'statistics.json').read_text()))


def test_rejects_tampered_donor_padj(tmp_path):
    scrna = build_valid_outputs(tmp_path)

    def tamper(frame):
        frame.loc[0, 'donor_padj'] = 0.95
    _rewrite(scrna/'axis_localization.csv', tamper)
    with pytest.raises(ValueError):
        validate_scrna_outputs(scrna, json.loads((scrna/'statistics.json').read_text()))


def test_rejects_duplicate_gene_celltype_rows(tmp_path):
    scrna = build_valid_outputs(tmp_path)

    def duplicate(frame):
        frame.loc[len(frame)] = frame.iloc[0]
    _rewrite(scrna/'axis_localization.csv', duplicate)
    with pytest.raises(ValueError):
        validate_scrna_outputs(scrna, json.loads((scrna/'statistics.json').read_text()))


# --------------------------------------------------------------------------------------
# deconvolution paired estimates
# --------------------------------------------------------------------------------------
def test_rejects_tampered_paired_padj(tmp_path):
    scrna = build_valid_outputs(tmp_path)

    def tamper(frame):
        frame.loc[0, 'padj'] = 0.99
    _rewrite(scrna/'deconvolution/deconvolution_paired.csv', tamper)
    with pytest.raises(ValueError):
        validate_scrna_outputs(scrna, json.loads((scrna/'statistics.json').read_text()))


def test_rejects_paired_rows_without_estimate_label(tmp_path):
    scrna = build_valid_outputs(tmp_path)

    def relabel(frame):
        frame.loc[0, 'claim_type'] = 'measured_fraction'
    _rewrite(scrna/'deconvolution/deconvolution_paired.csv', relabel)
    with pytest.raises(ValueError):
        validate_scrna_outputs(scrna, json.loads((scrna/'statistics.json').read_text()))


def test_rejects_paired_rows_without_method_label(tmp_path):
    scrna = build_valid_outputs(tmp_path)

    def clear(frame):
        frame.loc[0, 'method'] = ''
    _rewrite(scrna/'deconvolution/deconvolution_paired.csv', clear)
    with pytest.raises(ValueError):
        validate_scrna_outputs(scrna, json.loads((scrna/'statistics.json').read_text()))


# --------------------------------------------------------------------------------------
# estimated proportions
# --------------------------------------------------------------------------------------
def test_rejects_negative_proportions(tmp_path):
    scrna = build_valid_outputs(tmp_path)

    def make_negative(frame):
        frame.loc[0, 'CD8T cell'] = -0.05
    _rewrite(scrna/'deconvolution/cell_proportions_pancancer.csv', make_negative)
    with pytest.raises(ValueError):
        validate_scrna_outputs(scrna, json.loads((scrna/'statistics.json').read_text()))


def test_rejects_nonfinite_proportions(tmp_path):
    scrna = build_valid_outputs(tmp_path)

    def make_nan(frame):
        frame.loc[0, 'CD8T cell'] = np.nan
    _rewrite(scrna/'deconvolution/cell_proportions_pancancer.csv', make_nan)
    with pytest.raises(ValueError):
        validate_scrna_outputs(scrna, json.loads((scrna/'statistics.json').read_text()))


def test_rejects_proportions_that_do_not_sum_to_one(tmp_path):
    scrna = build_valid_outputs(tmp_path)

    def break_sum(frame):
        frame.loc[0, 'Malignant cell'] = 0.90  # 0.30 + 0.90 = 1.20
    _rewrite(scrna/'deconvolution/cell_proportions_pancancer.csv', break_sum)
    with pytest.raises(ValueError):
        validate_scrna_outputs(scrna, json.loads((scrna/'statistics.json').read_text()))


def test_rejects_proportion_table_without_estimate_label(tmp_path):
    scrna = build_valid_outputs(tmp_path)

    def relabel(frame):
        frame['claim_type'] = 'measured_fraction'
    _rewrite(scrna/'deconvolution/cell_proportions_pancancer.csv', relabel)
    with pytest.raises(ValueError):
        validate_scrna_outputs(scrna, json.loads((scrna/'statistics.json').read_text()))


# --------------------------------------------------------------------------------------
# signature matrices and audits
# --------------------------------------------------------------------------------------
def test_rejects_negative_signature_values(tmp_path):
    scrna = build_valid_outputs(tmp_path)

    def make_negative(frame):
        frame.loc[0, 'CD8T cell'] = -3.0
    _rewrite(scrna/'deconvolution/signature_matrix_pancancer.csv', make_negative)
    with pytest.raises(ValueError):
        validate_scrna_outputs(scrna, json.loads((scrna/'statistics.json').read_text()))


def test_rejects_nonfinite_signature_values(tmp_path):
    scrna = build_valid_outputs(tmp_path)

    def make_inf(frame):
        frame.loc[1, 'Malignant cell'] = np.inf
    _rewrite(scrna/'deconvolution/signature_matrix_pancancer.csv', make_inf)
    with pytest.raises(ValueError):
        validate_scrna_outputs(scrna, json.loads((scrna/'statistics.json').read_text()))


def test_rejects_forced_zero_entries_in_signature_audit(tmp_path):
    scrna = build_valid_outputs(tmp_path)

    def record_forced_zeros(frame):
        frame.loc[0, 'n_forced_zero_entries'] = 3
    _rewrite(scrna/'deconvolution/signature_matrix_audit.csv', record_forced_zeros)
    with pytest.raises(ValueError):
        validate_scrna_outputs(scrna, json.loads((scrna/'statistics.json').read_text()))


def test_rejects_signature_audit_without_cpm_scale(tmp_path):
    scrna = build_valid_outputs(tmp_path)

    def mislabel(frame):
        frame['scale'] = 'logcpm'
    _rewrite(scrna/'deconvolution/signature_matrix_audit.csv', mislabel)
    with pytest.raises(ValueError):
        validate_scrna_outputs(scrna, json.loads((scrna/'statistics.json').read_text()))


def test_rejects_missing_reference_audit_file(tmp_path):
    scrna = build_valid_outputs(tmp_path)
    (scrna/'deconvolution/reference_audit.csv').unlink()
    with pytest.raises((ValueError, FileNotFoundError)):
        validate_scrna_outputs(scrna, json.loads((scrna/'statistics.json').read_text()))


# --------------------------------------------------------------------------------------
# descriptive reference sensitivity
# --------------------------------------------------------------------------------------
def test_rejects_non_descriptive_reference_sensitivity_claim(tmp_path):
    scrna = build_valid_outputs(tmp_path)

    def relabel(frame):
        frame['claim_type'] = 'inferential_reference_validation'
    _rewrite(scrna/'deconvolution/reference_sensitivity.csv', relabel)
    with pytest.raises(ValueError):
        validate_scrna_outputs(scrna, json.loads((scrna/'statistics.json').read_text()))


def test_rejects_negative_reference_sensitivity_difference(tmp_path):
    scrna = build_valid_outputs(tmp_path)

    def break_difference(frame):
        frame.loc[0, 'mean_abs_difference'] = -0.1
    _rewrite(scrna/'deconvolution/reference_sensitivity.csv', break_difference)
    with pytest.raises(ValueError):
        validate_scrna_outputs(scrna, json.loads((scrna/'statistics.json').read_text()))


def test_rejects_out_of_range_direction_concordance(tmp_path):
    scrna = build_valid_outputs(tmp_path)

    def break_concordance(frame):
        frame.loc[0, 'direction_concordance'] = 1.5
    _rewrite(scrna/'deconvolution/reference_sensitivity.csv', break_concordance)
    with pytest.raises(ValueError):
        validate_scrna_outputs(scrna, json.loads((scrna/'statistics.json').read_text()))

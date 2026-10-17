"""Pre-registered candidate-axis prognosis branch (tumor values only, triple109).

Cohort tracking, not discovery: the catecholamine-3mt axis and its feature panel are
declared in knowledge/tme_metabolism_axes.yaml before execution, thresholds are frozen
in config/study_config.yaml, and every tested feature is registered - including
not-estimable rows. Survival associations remain observational single-cohort evidence;
deconvolution proportions are external-atlas estimates and never count toward the
two-measured-omics rule.
"""
from __future__ import annotations
import logging
from pathlib import Path
import pandas as pd
import yaml
from .io import artifact, read_json, write_json, sha256
from .statistics import invoke_r
from .checkers import validate_prognosis_outputs, validate_schema

LOG = logging.getLogger(__name__)


def _feature_index(path):
    if not Path(path).exists():
        return set()
    return set(pd.read_csv(path, usecols=[0]).iloc[:, 0].astype(str))


def _signature_symbols(gmt_path):
    symbols = set()
    for line in Path(gmt_path).read_text(encoding='utf-8').splitlines():
        fields = line.split('\t')
        if len(fields) > 2:
            symbols.update(g for g in fields[2:] if g.strip())
    return sorted(symbols)


def analyze_prognosis(root, run, cfg, scrna_result=None):
    root, run = Path(root), Path(run)
    result_path = run / 'prognosis/result.json'
    if result_path.exists():
        previous = read_json(result_path)
        if previous['status'] in {'complete', 'review_only'}:
            for a in previous['artifacts']:
                if sha256(run / a['path']) != a['sha256']:
                    raise ValueError(f'Cached artifact changed: {a["path"]}')
            return previous
    (run / 'prognosis').mkdir(parents=True, exist_ok=True)
    pcfg = cfg['analysis']['prognosis']
    tcfg = cfg['analysis']['tcell']
    axes = yaml.safe_load((root / 'knowledge/tme_metabolism_axes.yaml').read_text())['axes']
    axis = next(a for a in axes if a['axis'] == pcfg['axis'])
    errors, stats = [], {}
    try:
        audit_path = run / 'pathways/transcriptomics/triple109/gene_mapping_audit.csv'
        if not audit_path.exists():
            raise FileNotFoundError('gene_mapping_audit.csv missing; transcriptomics pathway stage did not complete')
        audit = pd.read_csv(audit_path)
        audit = audit.loc[audit.selected_representative.fillna(False) & audit.symbol.notna()]
        symbol_to_rna = dict(zip(audit.symbol.astype(str), audit.feature_id.astype(str)))
        annotations = pd.read_csv(run / 'inputs/metabolite_annotations.csv')
        met_index = _feature_index(run / 'metabolomics/triple109/expression.csv')
        prot_index = _feature_index(run / 'proteomics/triple109/expression.csv')
        features, missing = [], []
        for gene in axis['genes']:
            if gene in symbol_to_rna:
                features.append({'feature_id': symbol_to_rna[gene], 'omics': 'transcriptomics',
                                 'entity': gene, 'axis': axis['axis']})
            else:
                missing.append({'entity': gene, 'omics': 'transcriptomics', 'reason': 'not_in_representative_mapping'})
            if gene in prot_index:
                features.append({'feature_id': gene, 'omics': 'proteomics', 'entity': gene, 'axis': axis['axis']})
            else:
                missing.append({'entity': gene, 'omics': 'proteomics', 'reason': 'not_in_protein_matrix'})
        for compound in axis['compounds']:
            hits = annotations.loc[annotations.compound_id == compound, 'feature_id'].astype(str).unique()
            present = [f for f in hits if f in met_index]
            for feature in present:
                features.append({'feature_id': feature, 'omics': 'metabolomics', 'entity': compound, 'axis': axis['axis']})
            if not present:
                missing.append({'entity': compound, 'omics': 'metabolomics', 'reason': 'peak_absent_or_qc_excluded'})
        if not features:
            raise ValueError('No pre-registered axis feature is measurable in this run')
        gmt = root / 'knowledge/resources/tcell_signatures.gmt'
        signature_features = [{'symbol': s, 'feature_id': symbol_to_rna[s]}
                              for s in _signature_symbols(gmt) if s in symbol_to_rna]
        proportions = {panel: str(run / f'scrna/deconvolution/cell_proportions_{panel}.csv')
                       for panel in tcfg['panels']
                       if (run / f'scrna/deconvolution/cell_proportions_{panel}.csv').exists()}
        expression = {'transcriptomics': str(run / 'transcriptomics/triple109/expression.csv'),
                      'proteomics': str(run / 'proteomics/triple109/expression.csv'),
                      'metabolomics': str(run / 'metabolomics/triple109/expression.csv')}
        sample_metadata = {omics: str(run / omics / 'triple109/sample_metadata.csv') for omics in expression}
        job = {'omics': 'prognosis_candidate_axis', 'cohort_id': 'triple109', 'axis': axis['axis'],
               'features': features, 'missing_features': missing,
               'expression': expression, 'sample_metadata': sample_metadata,
               'clinical': str(run / 'inputs/clinical_analysis.csv'),
               'patients': str(run / 'inputs/patients.csv'),
               'same_batch_metadata': str(run / 'metabolomics/same_batch108/sample_metadata.csv'),
               'proportions': proportions, 'deconvolution_celltypes': tcfg['deconvolution_celltypes'],
               'signature_gmt': str(gmt), 'signature_features': signature_features,
               'signature_min_size': int(tcfg['signature_min_size']),
               'min_events': int(pcfg['min_events']), 'output_dir': str(run / 'prognosis'),
               'seed': cfg['study']['seed'], 'r_library_paths': cfg['runtime']['r_library_paths']}
        invoke_r(root, run, cfg, job, '06_prognosis.R')
        stats = read_json(run / 'prognosis/statistics.json')
        validate_prognosis_outputs(run / 'prognosis', stats)
    except Exception as exc:
        LOG.exception('Prognosis branch failed')
        errors.append({'step': 'prognosis', 'message': str(exc), 'resolved': False})
    paths = sorted(p for p in (run / 'prognosis').rglob('*') if p.is_file() and p != result_path)
    analyses = [{'name': 'cox_models', 'status': 'failed' if errors else 'complete',
                 'n_rows': int(stats.get('n_cox_rows', 0) or 0),
                 'method': 'coxph_per_sd_tumor_value_univariable_plus_adjusted_sensitivity',
                 'note': 'single-cohort observational association; PH diagnostics recorded, not used to switch models'},
                {'name': 'tcell_proportions', 'status': 'complete' if stats.get('n_patients_triple109') else 'skipped',
                 'n_rows': int(stats.get('n_patients_triple109', 0) or 0),
                 'method': 'nnls_deconvolution_estimates_from_external_atlas',
                 'note': 'estimated fractions, not measured cells; samples the same external atlas as the scrna branch'},
                {'name': 'tcell_signature', 'status': stats.get('signature_status', 'skipped'),
                 'n_rows': len(stats.get('signature_sets_used', []) or []),
                 'method': 'ssgsea_frozen_marker_sets_log2_normalized_counts',
                 'note': 'RNA-derived score sharing the RNA source; not an independent evidence layer'}]
    result = {'schema_version': '1.0', 'status': 'failed' if errors else 'review_only',
              'omics': 'prognosis_candidate_axis', 'cohort_id': ['triple109', 'same_batch108']
              if stats.get('n_patients_same_batch108') else ['triple109'],
              'scope': 'pre_registered_candidate_axis_catecholamine_3mt_tumor_values_only',
              'evidence_boundary': 'candidate_axis_tracking_prespecified_not_discovery',
              'axis': axis['axis'], 'n_patients': int(stats.get('n_patients_triple109', 0) or 0),
              'n_events_os': int(stats.get('n_events_os_triple109', 0) or 0),
              'n_events_rfs': int(stats.get('n_events_rfs_triple109', 0) or 0),
              'n_cox_rows': int(stats.get('n_cox_rows', 0) or 0),
              'analyses': analyses,
              'artifacts': [artifact(run, p) for p in paths],
              'qc': ['tumor_values_only_no_T_minus_N_delta_in_this_branch',
                     'thresholds_and_feature_panel_frozen_before_execution',
                     'deconvolution_proportions_are_external_atlas_estimates_not_measured_fractions',
                     'rna_signature_score_shares_the_rna_source_and_is_not_independent_evidence',
                     'three_metoxytyramine_peak_identity_remains_ambiguous_until_targeted_validation',
                     'not_estimable_models_are_retained_not_deleted'],
              'errors': errors}
    validate_schema(result, root / 'schemas/prognosis_output.schema.json')
    write_json(result_path, result)
    if not errors and not scrna_result:
        LOG.info('Prognosis branch ran without scrna context; proportion analyses may be absent')
    return result

"""External scRNA atlas context branch. Deterministic tool dispatch; evidence-bounded.

The atlas is an external pan-cancer object; donors differ from the bulk cohort.
Outputs are context/descriptive plus donor-level tests and never count as
independent validation or toward the two-measured-omics hypothesis rule.
"""
from __future__ import annotations
import logging
from pathlib import Path
import yaml
from .io import artifact, read_json, write_json, sha256
from .statistics import invoke_r
from .checkers import validate_scrna_outputs

LOG = logging.getLogger(__name__)


def analyze_scrna(root, run, cfg, rna_result=None, protein_result=None):
    root, run = Path(root), Path(run)
    result_path = run / 'scrna/result.json'
    if result_path.exists():
        previous = read_json(result_path)
        if previous['status'] in {'complete', 'review_only'}:
            for a in previous['artifacts']:
                if sha256(run/a['path']) != a['sha256']:
                    raise ValueError(f'Cached artifact changed: {a["path"]}')
            return previous
    (run/'scrna').mkdir(parents=True, exist_ok=True)
    scfg = cfg['analysis']['scrna']
    axes = yaml.safe_load((root/'knowledge/tme_metabolism_axes.yaml').read_text())['axes']
    axis_genes = sorted({g for a in axes for g in a['genes']})

    def de_path(result, *parts):
        if result is None or result['status'] not in {'complete', 'review_only'}:
            return None
        p = run.joinpath(*parts)
        return str(p) if p.exists() else None

    rna_de = {cohort: de_path(rna_result, 'transcriptomics', cohort, 'de.csv') or None
              for cohort in ['full159', 'triple109']}
    errors = []
    stats = {}
    try:
        job = {'omics': 'scrna_context', 'cohort_id': 'external_atlas',
               'rds_path': str(root/cfg['inputs']['scrna_object']),
               'assay': scfg['assay'], 'celltype_column': scfg['celltype_column'],
               'sample_column': scfg['sample_column'], 'source_column': scfg['source_column'],
               'min_cells_per_group': scfg['min_cells_per_group'],
               'min_cells_per_celltype': scfg['min_cells_per_celltype'],
               'min_samples_per_celltype': scfg['min_samples_per_celltype'],
               'marker_top_n': scfg['marker_top_n'], 'marker_min_pct': scfg['marker_min_pct'],
               'marker_sensitivity_top_n': scfg['marker_sensitivity_top_n'],
               'umap_figure_cell_cap': scfg['umap_figure_cell_cap'],
               'deconvolution': {'matched_reference_sensitivity': scfg['deconvolution']['matched_reference_sensitivity']},
               'genesets': {'min_size': scfg['genesets']['min_size']},
               'axis_genes': axis_genes, 'rna_de': rna_de,
               'prot_de': de_path(protein_result, 'proteomics', 'triple109', 'de.csv'),
               'bulk_counts': str(run/'inputs/transcriptomics.csv'),
               'sample_registry': str(run/'inputs/sample_registry.csv'),
               'clinical': str(run/'inputs/clinical_analysis.csv'),
               'gene_mapping': str(root/'knowledge/resources/gene_mapping.csv'),
               'output_dir': str(run/'scrna'), 'seed': cfg['study']['seed'],
               'r_library_paths': cfg['runtime']['r_library_paths']}
        invoke_r(root, run, cfg, job, '05_scrna_celltype.R')
        stats = read_json(run/'scrna/statistics.json')
        validate_scrna_outputs(run/'scrna', stats)
    except Exception as exc:
        LOG.exception('scRNA branch failed')
        errors.append({'step': 'scrna', 'message': str(exc), 'resolved': False})
    paths = sorted(p for p in (run/'scrna').rglob('*') if p.is_file() and p != result_path)
    result = {'schema_version': '1.0', 'status': 'failed' if errors else 'review_only',
              'omics': 'scrna_context', 'cohort_id': ['external_atlas'], 'scope': scfg['scope'],
              'evidence_boundary': 'external_context_not_independent_validation',
              'n_cells': int(stats.get('n_cells', 0)), 'n_samples': int(stats.get('n_samples', 0)),
              'n_celltypes': int(stats.get('n_celltypes', 0)),
              'analyses': stats.get('analyses', []),
              'artifacts': [artifact(run, p) for p in paths],
              'qc': ['external_pan_cancer_atlas_donors_differ_from_bulk_cohort',
                     'donor_is_the_statistical_unit_for_pseudobulk_tests',
                     'deconvolution_proportions_are_estimates_not_measured_fractions',
                     'no_refiltering_of_external_object_descriptive_qc_only'],
              'errors': errors}
    write_json(result_path, result)
    return result

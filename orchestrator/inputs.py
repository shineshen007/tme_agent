"""Cohort-specific import adapter; explicit IDs, immutable raw inputs."""
from __future__ import annotations
import csv
import logging
import re
from pathlib import Path
import numpy as np
import pandas as pd
from .io import artifact, project_file, write_json

LOG = logging.getLogger(__name__)
TUMOR = 'Tumor (T) sample ID'
ADJACENT = 'Adjacent liver tissue (N) sample ID'
CIRRHOSIS = 'Liver cirrhosis (1, yes; 0, no)'
TNM = 'TNM stage'
TNM_ORDINALS = {'I': 1, 'II': 2, 'III': 3, 'IV': 4}


def tnm_ordinal(value):
    """Roman TNM stage to ordinal I-IV; unrecognized values stay unmapped (never guessed)."""
    text = re.sub(r'^stage\s*', '', str(value).strip(), flags=re.IGNORECASE).upper()
    for roman in sorted(TNM_ORDINALS, key=len, reverse=True):
        if text.startswith(roman):
            return TNM_ORDINALS[roman]
    return None


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_table(path):
    with Path(path).open(encoding='utf-8-sig', newline='') as f:
        header = next(csv.reader(f))
    require(len(set(header)) == len(header), f'Duplicate columns: {path}')
    return pd.read_csv(path, dtype=str, keep_default_na=False, encoding='utf-8-sig')


def unique(frame, column, label):
    require(column in frame, f'{label}: missing {column}')
    require(frame[column].str.strip().ne('').all(), f'{label}: blank {column}')
    require(frame[column].is_unique, f'{label}: duplicate {column}')


def numeric(frame, label, nonnegative=False, integer=False):
    x = frame.apply(pd.to_numeric, errors='coerce')
    require(np.isfinite(x.to_numpy()).all(), f'{label}: missing/nonfinite/nonnumeric values')
    if nonnegative:
        require((x.to_numpy() >= 0).all(), f'{label}: negative values')
    if integer:
        require(np.equal(x.to_numpy(), np.floor(x.to_numpy())).all(), f'{label}: fractional counts')
        require(x.to_numpy().max() <= np.iinfo(np.int32).max, f'{label}: count exceeds R integer range')
        x = x.astype(np.int32)
    return x


def equivalent(a, b):
    a, b = str(a).strip(), str(b).strip()
    missing = {'', 'NA', 'NaN', 'nan'}
    if a in missing and b in missing:
        return True
    try:
        return bool(np.isclose(float(a), float(b), atol=0.01, rtol=0))
    except ValueError:
        return a == b


def met_name(source):
    m = re.fullmatch(r'M_(\d+)', source)
    require(m is not None, f'Invalid metabolomics sample identifier: {source}')
    return f'M ({m[1]})'


def build_registry(full, subset, sample_full, sample_met, cfg):
    unique(full, TUMOR, 'clinic_full')
    unique(full, ADJACENT, 'clinic_full')
    unique(subset, 'T_ID', 'clinic_subset')
    unique(sample_full, 'sample.name', 'sample_full')
    unique(sample_met, 'sample.name', 'sample_met')
    require(cfg['mapping']['pairing'] == 'confirmed_within_group_row_order', 'Pairing rule not authorized')
    require(set(sample_full['group']) == {'Tumor', 'Non_Tumor'}, 'Unexpected RNA tissue labels')
    tumors = sample_full.loc[sample_full.group == 'Tumor', 'sample.name'].tolist()
    adjacent = sample_full.loc[sample_full.group == 'Non_Tumor', 'sample.name'].tolist()
    require(len(tumors) == len(adjacent) == len(full), 'RNA pair count mismatch')
    require(set(tumors) == set(full[TUMOR]), 'RNA tumor IDs do not match clinical patients')
    pair = dict(zip(tumors, adjacent))  # User explicitly confirmed within-group ordering.
    require(set(subset.T_ID) <= set(full[TUMOR]), 'Subset has unknown patients')
    require((subset.T_ID == subset[TUMOR]).all(), 'Subset patient keys conflict')
    require(all(pair[r.T_ID] == r.N_ID for r in subset.itertuples()), '109 RNA pair mappings disagree')
    require(len(full) == cfg['study']['n_pairs'], 'Unexpected full cohort size')
    require(len(subset) == cfg['study']['n_complete_omics_pairs'], 'Unexpected triple cohort size')
    si = sample_met.set_index('sample.name')
    inverse_alias = {v: k for k, v in cfg['mapping']['rna_count_aliases'].items()}
    require(len(inverse_alias) == len(cfg['mapping']['rna_count_aliases']), 'Alias target collision')
    sub = subset.set_index('T_ID')
    records = []
    for row in full.to_dict('records'):
        patient = row[TUMOR]
        for tissue, specimen, rna_id in [('tumor', row[TUMOR], patient), ('adjacent', row[ADJACENT], pair[patient])]:
            base = {'patient_id': patient, 'biospecimen_id': specimen, 'tissue': tissue,
                    'in_triple109': patient in sub.index, 'sample_type': 'biological',
                    'pairing_authority': cfg['mapping']['authority'], 'batch': '', 'injection_order': ''}
            records.append({**base, 'omics': 'transcriptomics', 'assay_sample_id': inverse_alias.get(rna_id, rna_id), 'source_metadata_id': rna_id})
            records.append({**base, 'omics': 'proteomics', 'assay_sample_id': specimen, 'source_metadata_id': specimen})
            if patient in sub.index:
                source = met_name(sub.loc[patient, 'ID' if tissue == 'tumor' else 'ID_Pair'])
                require(source in si.index, f'Missing metabolomics sample {source}')
                expected = 'Tumor' if tissue == 'tumor' else 'Non_Tumor'
                require(si.loc[source, 'group'] == expected and si.loc[source, 'class'] == 'Subject', f'Metabolomics tissue conflict: {source}')
                records.append({**base, 'omics': 'metabolomics', 'assay_sample_id': source, 'source_metadata_id': source,
                                'batch': si.loc[source, 'batch'], 'injection_order': si.loc[source, 'injection.order']})
    registry = pd.DataFrame(records)
    require(not registry.duplicated(['omics', 'assay_sample_id']).any(), 'Non-bijective sample mapping')
    for (_, omics), group in registry.groupby(['patient_id', 'omics']):
        require(len(group) == 2 and set(group.tissue) == {'tumor', 'adjacent'}, f'Incomplete pair: {omics}')
    biological = set(sample_met.loc[sample_met['class'] == 'Subject', 'sample.name'])
    require(biological == set(registry.loc[registry.omics == 'metabolomics', 'assay_sample_id']), 'Unmapped metabolomics subjects')
    require(set(sample_met['class']) <= {'Subject', 'QC'}, 'Unknown sample types')
    return registry


def split_metabolites(table):
    annotations = ['name', 'class', 'mz', 'rt', 'Annotation.type', 'ID', 'score', 'isotope', 'adduct', 'compound.name', 'confidence']
    require(set(annotations) <= set(table), 'Missing metabolomics annotation columns')
    sample_columns = [c for c in table if c not in annotations]
    for feature, group in table.groupby('name', sort=False):
        require(len(group[['mz', 'rt'] + sample_columns].drop_duplicates()) == 1, f'Conflicting profiles for feature {feature}')
    first = table.drop_duplicates('name').set_index('name')
    matrix = numeric(first[sample_columns], 'metabolomics', nonnegative=True)
    require((matrix.to_numpy() > 0).all(), 'Zero intensity requires an explicit missing-value policy')
    matrix.index.name = 'feature_id'
    ann = table[annotations].rename(columns={'name': 'feature_id', 'ID': 'compound_id', 'compound.name': 'compound_name', 'Annotation.type': 'annotation_type', 'confidence': 'source_grade'})
    ann.insert(0, 'annotation_id', [f'annotation_{i:06d}' for i in range(1, len(ann) + 1)])
    ann['candidate_count'] = ann.groupby('feature_id').compound_id.transform('nunique')
    ann['identity_status'] = np.where(ann.candidate_count > 1, 'ambiguous', 'putative_unverified')
    ann['annotation_system'] = 'MetDNA_grade_unverified_provenance'
    ann['msi_level'] = ''
    return matrix, ann


def prepare_inputs(root, run, cfg):
    run = Path(run)
    out = run / 'inputs'
    out.mkdir(parents=True, exist_ok=False)
    tables = {k: read_table(project_file(root, cfg['inputs'][k])) for k in ['clinic_full', 'clinic_subset', 'sample_full', 'sample_met']}
    full, subset = tables['clinic_full'], tables['clinic_subset']
    registry = build_registry(full, subset, tables['sample_full'], tables['sample_met'], cfg)
    registry.to_csv(out / 'sample_registry.csv', index=False)
    pairs = full[[TUMOR, ADJACENT]].rename(columns={TUMOR: 'patient_id', ADJACENT: 'adjacent_specimen'})
    pairs['tumor_specimen'] = pairs.patient_id
    pairs['in_triple109'] = pairs.patient_id.isin(subset.T_ID)
    pairs.to_csv(out / 'patients.csv', index=False)
    # Preserve the authoritative original clinical fields, and isolate historical derived analyses.
    full.to_csv(out / 'clinical_authoritative.csv', index=False)
    derived_columns = [c for c in subset if c not in full]
    subset[['T_ID'] + [c for c in derived_columns if c != 'T_ID']].to_csv(out / 'historical_derived.csv', index=False)
    fi = full.set_index(TUMOR)
    conflicts = []
    for row in subset.to_dict('records'):
        for column in sorted(set(full) & set(subset)):
            a, b = fi.loc[row['T_ID'], column] if column != TUMOR else row['T_ID'], row[column]
            if not equivalent(a, b):
                authorized = column in cfg['mapping']['clinical_conflict_fields']
                conflicts.append({'patient_id': row['T_ID'], 'field': column, 'clinic_full': a, 'clinic_subset': b,
                                  'resolution': 'clinic_full_user_authorized' if authorized else 'unresolved'})
    pd.DataFrame(conflicts, columns=['patient_id', 'field', 'clinic_full', 'clinic_subset', 'resolution']).to_csv(out / 'clinical_conflicts.csv', index=False)
    require(all(c['resolution'] != 'unresolved' for c in conflicts), 'Unresolved clinical source conflict')
    clinical = pd.DataFrame({'patient_id': full[TUMOR], 'age': full['Age'], 'sex': full['Gender'],
                             'cirrhosis': full[CIRRHOSIS], 'os_months': full['Overall survial (month)'],
                             'os_event': full['Survial  (1, dead; 0, alive)'],
                             'rfs_months': full['Recurrence-free survival (month)'], 'rfs_event': full['Recurrence  (1, yes; 0, no)'],
                             'tumor_purity': full['Tumor purity by HE staining'].str.rstrip('%').astype(float) / 100,
                             'tnm_stage': full[TNM].str.strip(),
                             'tnm_ordinal': [tnm_ordinal(v) for v in full[TNM]]})
    clinical.to_csv(out / 'clinical_analysis.csv', index=False)
    excluded = []
    counts = {}
    for omics, key in [('transcriptomics', 'rna_counts'), ('proteomics', 'protein')]:
        table = read_table(project_file(root, cfg['inputs'][key]))
        id_column = table.columns[0]
        unique(table, id_column, key)
        if omics == 'transcriptomics':
            for feature in cfg['mapping']['allowed_invalid_count_rows']:
                row = table.loc[table[id_column] == feature]
                if len(row):
                    require(row.iloc[:, 1:].isin(['NA', '', 'NaN']).all().all(), 'Authorized invalid row has numeric data')
                    excluded.append({'omics': omics, 'feature_id': feature, 'reason': 'all_missing_non_gene_row'})
                    table = table.loc[table[id_column] != feature]
        m = table.set_index(id_column)
        reg = registry.loc[registry.omics == omics].sort_values(['patient_id', 'tissue'])
        require(set(m.columns) == set(reg.assay_sample_id), f'{omics}: unmapped or missing matrix sample')
        m = numeric(m.loc[:, reg.assay_sample_id], omics, nonnegative=omics == 'transcriptomics', integer=omics == 'transcriptomics')
        m.columns = reg.biospecimen_id
        m.index.name = 'feature_id'
        m.to_csv(out / f'{omics}.csv')
        counts[omics] = {'n_features': len(m), 'n_samples': m.shape[1], 'n_pairs': reg.patient_id.nunique()}
        LOG.info('%s: %s features x %s samples', omics, *m.shape)
    matrix, ann = split_metabolites(read_table(project_file(root, cfg['inputs']['metabolomics'])))
    require(set(matrix.columns) == set(tables['sample_met']['sample.name']), 'Metabolomics sample columns differ from injection metadata')
    metreg = registry.loc[registry.omics == 'metabolomics'].sort_values(['patient_id', 'tissue'])
    biological = matrix.loc[:, metreg.assay_sample_id].copy()
    biological.columns = metreg.biospecimen_id
    biological.to_csv(out / 'metabolomics.csv')
    matrix.to_csv(out / 'metabolomics_with_qc.csv')
    ann.to_csv(out / 'metabolite_annotations.csv', index=False)
    tables['sample_met'].to_csv(out / 'injection_metadata.csv', index=False)
    pd.DataFrame(excluded, columns=['omics', 'feature_id', 'reason']).to_csv(out / 'excluded_rows.csv', index=False)
    counts['metabolomics'] = {'n_features': len(matrix), 'n_samples': len(metreg), 'n_pairs': metreg.patient_id.nunique(),
                              'n_annotations': len(ann), 'n_qc': int((tables['sample_met']['class'] == 'QC').sum())}
    warnings = ['protein_centering_axis_unknown', 'metabolomics_processing_history_unknown', 'metabolite_identity_not_MSI_verified', 'bulk_tissue_does_not_establish_cell_origin_or_flux']
    if clinical.tnm_ordinal.isna().any():
        warnings.append('tnm_stage_unmapped_rows_present')
    manifest = {'schema_version': '1.0', 'status': 'complete', 'cohorts': counts, 'clinical_conflicts_resolved': len(conflicts),
                'tnm_stage_unmapped': int(clinical.tnm_ordinal.isna().sum()),
                'warnings': warnings, 'artifacts': [artifact(run, p) for p in sorted(out.glob('*.csv'))]}
    write_json(run / 'validation/validate_report.json', manifest)
    return manifest

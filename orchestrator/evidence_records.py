"""Lossless, read-only EvidenceRecord v2 projection of frozen registry rows.

No statistical test, threshold or evidence identifier is calculated here. Unknown
metadata stays explicit; empty relationship arrays mean unassessed, not absence.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

from .checkers import evidence_v2_validator
from .io import read_json


ENTITY_TYPES = {
    'transcriptomics': 'gene',
    'proteomics': 'protein',
    'metabolomics': 'metabolite_feature',
    'scrna_context': 'gene',
}


def _value(row, key, default='unknown'):
    value = row.get(key)
    if value is None or (not isinstance(value, (list, dict)) and pd.isna(value)):
        return default
    return str(value)


def _number(row, key, integer=False):
    value = row.get(key)
    if value is None or pd.isna(value):
        return None
    return int(value) if integer else float(value)


def _selected(row, external=False):
    if external:
        return 'not_applicable'
    value = row.get('selected')
    if value is None or pd.isna(value):
        raise ValueError(f'Missing selected for evidence {row.get("evidence_id")}')
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    raise ValueError(f'Non-boolean selected for evidence {row.get("evidence_id")}')


def project_evidence_v2(row, analysis_version='unknown', external=False, symbols=None, axes=None):
    """Copy a v1 row into typed groups; no inferred support or quality grade."""
    layer = 'scrna_context' if external else _value(row, 'evidence_layer')
    omics = 'scrna_context' if external else _value(row, 'omics')
    if layer not in {'de', 'prognosis', 'tcell_correlation', 'scrna_context'}:
        raise ValueError(f'Unsupported evidence layer: {layer}')
    if omics not in ENTITY_TYPES:
        raise ValueError(f'Unsupported omics: {omics}')
    entity_id = _value(row, 'entity' if external else 'feature_id')
    cohort = _value(row, 'cohort' if layer == 'prognosis' else 'cohort_id')
    if external:
        artifact = _value(row, 'artifact_path')
        row_key = f'{entity_id}|{_value(row, "celltype")}'
        direction = 'unknown'
        effect_scale = 'unknown'
        context = 'external_context'
        tissue = 'external_tumor_atlas'
        analysis = 'axis_localization'
    else:
        artifact = _value(row, 'artifact_path') if layer == 'de' else {
            'prognosis': 'integration/prognosis_association.csv',
            'tcell_correlation': 'integration/tcell_correlation.csv',
        }[layer]
        row_key = _value(row, 'row_key') if layer == 'de' else _value(row, 'evidence_id')
        direction = _value(row, 'direction', '')
        effect_scale = _value(row, 'effect_scale')
        context = 'measured_evidence' if layer == 'de' else 'derived_estimate'
        tissue = 'tumor_vs_adjacent' if layer == 'de' else 'tumor'
        analysis = _value(row, 'method') if layer == 'de' else layer
    symbol = _value(row, 'entity') if not external and layer != 'de' else 'unknown'
    if external:
        symbol = entity_id
    elif layer == 'de' and omics == 'proteomics':
        symbol = entity_id
    elif layer == 'de' and omics == 'transcriptomics' and symbols:
        symbol = symbols.get(entity_id, 'unknown')
    axis = _value(row, 'axis')
    if axis == 'unknown' and axes:
        axis = ';'.join(sorted(axes.get(_value(row, 'evidence_id'), []))) or 'unknown'
    record = {
        'schema_version': '2.0',
        'evidence_id': _value(row, 'evidence_id'),
        'source': {'omics': omics, 'cohort': cohort, 'assay': 'unknown',
                   'analysis': analysis, 'evidence_layer': layer},
        'biological_entity': {'entity_type': ENTITY_TYPES[omics], 'entity_id': entity_id,
                              'symbol': symbol, 'pathway': 'unknown',
                              'cell_type': _value(row, 'celltype', 'not_applicable') if external else 'not_applicable',
                              'tissue_context': tissue},
        'observation': {'direction': direction, 'selected': _selected(row, external),
                        'effect_scale': effect_scale, 'claim_type': _value(row, 'claim_type'),
                        'effect': _number(row, 'effect'), 'pvalue': _number(row, 'pvalue'),
                        'padj': _number(row, 'padj'), 'n_pairs': _number(row, 'n_pairs', integer=True)},
        'evidence_quality': {'measurement_reliability': 'unknown', 'statistical_stability': 'unknown',
                             'annotation_confidence': 'unknown', 'evidence_context': context},
        'relationship': {'axis': axis, 'supports': [], 'contradicts': []},
        'provenance': {'artifact_path': artifact, 'row_key': row_key,
                       'analysis_version': analysis_version},
    }
    return record


def write_evidence_registry_v2(root, run, registry, scrna_registry=None):
    """Write validated JSONL atomically after the unchanged v1 CSV is written."""
    root, run = Path(root), Path(run)
    schema = root / 'schemas/evidence_v2.schema.json'
    validator = evidence_v2_validator(schema)
    version_path = run / 'run.json'
    version = read_json(version_path).get('fingerprint', 'unknown') if version_path.exists() else 'unknown'
    symbols = {}
    audit = run / 'pathways/transcriptomics/triple109/gene_mapping_audit.csv'
    if audit.exists():
        frame = pd.read_csv(audit)
        if {'feature_id', 'symbol', 'selected_representative'} <= set(frame.columns):
            frame = frame.loc[frame.selected_representative.fillna(False) & frame.symbol.notna()]
            symbols = dict(zip(frame.feature_id.astype(str), frame.symbol.astype(str)))
    axes = {}
    axis_path = run / 'integration/axis_evidence.csv'
    if axis_path.exists():
        try:
            frame = pd.read_csv(axis_path)
        except pd.errors.EmptyDataError:
            frame = pd.DataFrame()
        if {'evidence_id', 'axis'} <= set(frame.columns):
            for eid, group in frame.dropna(subset=['evidence_id', 'axis']).groupby('evidence_id'):
                axes[str(eid)] = sorted(set(group.axis.astype(str)))
    path = run / 'integration/evidence_registry.v2.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    seen = set()
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, delete=False) as handle:
            temp = Path(handle.name)
            for frame, external in ((registry, False), (scrna_registry, True)):
                if frame is None:
                    continue
                for row in frame.to_dict('records'):
                    record = project_evidence_v2(row, version, external, symbols, axes)
                    eid = record['evidence_id']
                    if eid in seen:
                        raise ValueError(f'Duplicate v2 evidence_id: {eid}')
                    seen.add(eid)
                    validator.validate(record)
                    handle.write(json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(',', ':')) + '\n')
        os.replace(temp, path)
    except Exception:
        if 'temp' in locals():
            temp.unlink(missing_ok=True)
        raise
    return path

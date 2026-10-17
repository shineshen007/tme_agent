"""Target dossier stage: reproducible per-target evidence packages after target proposal.

Design rules (enforced in code, not only documented):
- Dossiers aggregate *frozen* evidence only: no new statistical test, no threshold change and no
  change of validation status (``not_independently_validated`` is a constant in the manifest).
- Every number rendered into a README, table or figure annotation is read from
  ``integration/evidence_registry.csv``; nothing is authored by a model.
- Negative, unselected, not-estimable and contradicting rows are retained in
  ``evidence/evidence_all_target.csv``; targets are never ranked.
- The module is standalone (stdlib + third-party only, no package-relative imports) so the same
  file can be copied to ``result/<slug>/code/dossiers.py`` and executed directly for reproduction.

CLI (also used by ``scripts/00_run.sh`` through ``orchestrator.runner``):
    build      build in-run dossiers at ``<run>/target_dossiers/<slug>/``
    publish    copy built dossiers to the project-root ``result/`` directory
    check      verify published dossiers (hashes, inputs, in-run copies, optional re-render)
    reproduce  per-target: re-render a dossier and compare byte-for-byte with its manifest
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

LOG = logging.getLogger(__name__)

SCHEMA_VERSION = '1.0'
MANIFEST_NAME = 'manifest.json'
CODE_COPY = 'code/dossiers.py'
REPRODUCE_SCRIPT = 'code/reproduce.sh'
EXCLUDED_FROM_CONTENT_HASH = {MANIFEST_NAME}
TARGETS_REL = 'report/agent/targets.json'
HYPOTHESES_REL = 'report/agent/hypotheses_agent.json'
REGISTRY_REL = 'integration/evidence_registry.csv'
AXIS_AUDIT_REL = 'pathways/transcriptomics/triple109/gene_mapping_audit.csv'
ANNOTATION_REL = 'inputs/metabolite_annotations.csv'
LITERATURE_REL = 'literature/records.json'
CLINICAL_REL = 'prognosis/patient_clinical.csv'
RNA_COHORTS = ['full159', 'triple109']
MET_COHORTS = ['triple109', 'same_batch108']
PLOT_COHORT = 'triple109'
RNA_THRESHOLD = 0.585
OTHER_THRESHOLD = 0.5
DEFAULT_RENDER = {'panels': ['de_effects', 'prognosis_forest', 'paired_tumor_adjacent', 'km_display'],
                  'formats': ['pdf', 'svg'], 'max_panels_per_page': 6,
                  'km_mode': 'median_split_display_only', 'km_outcomes': ['os', 'rfs'],
                  'include_model_validation_plan': True, 'include_literature_table': True}
BOUNDARY_KM = 'display_only_no_test'
BOUNDARY_TCell = 'estimate_or_rna_derived'
BOUNDARY_PROGNOSIS = 'single_cohort_observational_cox_no_competing_risks'
BOUNDARY_NESTING = 'triple109_subset_of_full159_not_independent'
FORBIDDEN_TARGET_FIELDS = {'priority', 'rank', 'score', 'order'}
LAYER_ORDER = {'de': 0, 'prognosis': 1, 'tcell_correlation': 2}
ROW_ORDER = ['evidence_layer', 'omics', 'cohort_id', 'outcome', 'model_type', 'readout_kind',
             'readout_panel', 'readout', 'feature_id', 'evidence_id']


# --------------------------------------------------------------------------- small helpers
def _now():
    return datetime.now(timezone.utc).isoformat()


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _artifact(base, path):
    path = Path(path).resolve()
    base = Path(base).resolve()
    return {'path': path.relative_to(base).as_posix(), 'sha256': _sha256(path),
            'bytes': path.stat().st_size}


def _write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as handle:
        handle.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
        temp = handle.name
    os.replace(temp, path)


def _read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _write_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')


def _write_csv(frame, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False, lineterminator='\n')


def _load_config(root):
    """Standalone config load: only the dossier block is needed off-pipeline."""
    import yaml
    path = Path(root) / 'config/study_config.yaml'
    cfg = yaml.safe_load(path.read_text(encoding='utf-8')) if path.exists() else {}
    local = Path(root) / 'config/runtime.local.yaml'
    if local.exists():
        cfg.setdefault('runtime', {}).update((yaml.safe_load(local.read_text(encoding='utf-8')) or {}).get('runtime', {}))
    return cfg


def render_settings(cfg):
    """Frozen render settings; recorded in the manifest so verification is independent of live config."""
    block = ((cfg or {}).get('deliverables', {}) or {}).get('dossiers', {}) or {}
    panels = list(block.get('panels') or DEFAULT_RENDER['panels'])
    formats = [str(f).lower() for f in (block.get('formats') or DEFAULT_RENDER['formats'])]
    if 'png' in formats:
        raise ValueError('Vector-only policy: PNG output is not allowed for dossiers')
    km = block.get('km') or {}
    return {'panels': panels, 'formats': formats,
            'max_panels_per_page': int(block.get('max_panels_per_page') or DEFAULT_RENDER['max_panels_per_page']),
            'km_mode': str(km.get('mode') or DEFAULT_RENDER['km_mode']),
            'km_outcomes': list(km.get('outcomes') or DEFAULT_RENDER['km_outcomes']),
            'include_model_validation_plan': bool(block.get('include_model_validation_plan', True)),
            'include_literature_table': bool(block.get('include_literature_table', True))}


def output_root(root, cfg):
    block = ((cfg or {}).get('deliverables', {}) or {}).get('dossiers', {}) or {}
    return Path(root) / str(block.get('output_dir') or 'result')


def _touch(used, path):
    path = Path(path)
    if path.exists() and path.is_file():
        used.add(path)
    return path


def _order_rows(frame):
    if not len(frame):
        return frame
    frame = frame.copy()
    layer = frame['evidence_layer'].fillna('de') if 'evidence_layer' in frame.columns else pd.Series('de', index=frame.index)
    frame['_layer_order'] = layer.astype(str).map(LAYER_ORDER).fillna(9)
    keys = ['_layer_order'] + [c for c in ROW_ORDER if c in frame.columns]
    return frame.sort_values(keys, kind='mergesort').drop(columns=['_layer_order'])


def _truthy(value):
    return str(value).strip().lower() in {'true', '1', 'yes', 't'}


def _fmt_effect(row):
    layer = str(row.get('evidence_layer') or 'de')
    effect = row.get('effect')
    if layer == 'prognosis' and (effect is None or pd.isna(effect)):
        # Prognosis rows carry the hazard ratio in its own column; the registry leaves effect empty.
        effect = row.get('hr')
    if effect is None or pd.isna(effect):
        return 'HR NA' if layer == 'prognosis' else 'NA'
    effect = float(effect)
    if layer == 'prognosis':
        if pd.notna(row.get('ci_low')) and pd.notna(row.get('ci_high')):
            return f'HR {effect:.3f} ({float(row.ci_low):.3f}-{float(row.ci_high):.3f})'
        return f'HR {effect:.3f}'
    if layer == 'tcell_correlation':
        return f'rho {effect:+.3f}'
    return f'{effect:+.3f}'


def _cohort_value(row):
    def missing(value):
        return value is None or pd.isna(value) or str(value).strip() in {'', 'nan'}
    value = row.get('cohort_id')
    if missing(value):
        value = row.get('cohort')
    return '' if missing(value) else str(value)


def _fmt_p(value):
    return f'{float(value):.2e}' if value is not None and pd.notna(value) else 'NA'


def _fmt_int(value):
    return str(int(value)) if value is not None and pd.notna(value) else 'NA'


def _md_table(header, rows):
    lines = ['| ' + ' | '.join(header) + ' |', '|' + '|'.join(['---'] * len(header)) + '|']
    for row in rows:
        lines.append('| ' + ' | '.join('' if cell is None else str(cell) for cell in row) + ' |')
    return lines


def _slugify(identifier, used):
    base = ''.join(ch if (ch.isalnum() or ch in '._-') else '_' for ch in str(identifier)).strip('._-')
    slug = base or 'target'
    index = 2
    while slug in used:
        slug = f'{base}-{index}'
        index += 1
    used.add(slug)
    return slug


# --------------------------------------------------------------------------- matplotlib style
def resolve_font():
    """Arial when present, otherwise the metric-compatible Liberation Sans (recorded in the manifest)."""
    from matplotlib import font_manager
    for name in ['Arial', 'Liberation Sans']:
        try:
            font_manager.findfont(name, fallback_to_default=False)
            return name, name != 'Arial'
        except ValueError:
            continue
    raise ValueError('Neither Arial nor metric-compatible Liberation Sans is available')


def setup_matplotlib(root):
    os.environ.setdefault('MPLCONFIGDIR', str(Path(root) / '.cache/matplotlib'))
    os.environ.setdefault('SOURCE_DATE_EPOCH', '1700000000')
    import matplotlib
    matplotlib.use('Agg')
    font, substituted = resolve_font()
    matplotlib.rcParams.update({'font.family': font, 'font.size': 8, 'pdf.fonttype': 42,
                                'ps.fonttype': 42, 'svg.fonttype': 'none', 'svg.hashsalt': 'hcc-tme-dossiers-v1',
                                'axes.spines.top': False, 'axes.spines.right': False,
                                'figure.max_open_warning': 0})
    return matplotlib, font, substituted


def _normalize_svg(path):
    """Rewrite matplotlib's object-id-derived element ids to first-appearance names.

    matplotlib ids (``p…``/``m…``) hash Python object identity, so byte-identical reproduction
    requires renaming them deterministically; the drawing order is deterministic.
    """
    path = Path(path)
    text = path.read_text(encoding='utf-8')
    mapping = {}
    for match in re.finditer(r'\b([pm][0-9a-f]{10,})\b', text):
        mapping.setdefault(match.group(1), f'ref{len(mapping)}')
    for token, replacement in mapping.items():
        text = text.replace(token, replacement)
    path.write_text(text, encoding='utf-8')


def _save_figure(fig, base, formats):
    """Deterministic vector export: no embedded dates, fixed ids (see _normalize_svg)."""
    import matplotlib.pyplot as plt
    paths = []
    for extension in formats:
        target = Path(f'{base}.{extension}')
        if extension == 'pdf':
            fig.savefig(target, metadata={'CreationDate': None, 'ModDate': None})
        elif extension == 'svg':
            fig.savefig(target, metadata={'Date': None})
            _normalize_svg(target)
        else:
            fig.savefig(target)
        paths.append(target)
    plt.close(fig)
    return paths


# --------------------------------------------------------------------------- target resolution
def load_targets(run):
    path = Path(run) / TARGETS_REL
    if not path.exists():
        return None
    payload = _read_json(path)
    for group in payload.get('groups', []):
        for target in group.get('targets', []):
            offending = FORBIDDEN_TARGET_FIELDS & set(target)
            if offending:
                raise ValueError(f'Target {target.get("identifier")} carries ranking fields: {sorted(offending)}')
    return payload


def load_axes(root):
    import yaml
    path = Path(root) / 'knowledge/tme_metabolism_axes.yaml'
    return yaml.safe_load(path.read_text(encoding='utf-8'))['axes'] if path.exists() else []


def load_registry(run):
    path = Path(run) / REGISTRY_REL
    if not path.exists():
        raise FileNotFoundError(f'Evidence registry missing: {path}')
    return pd.read_csv(path, low_memory=False)


def _symbol_map(run, used):
    path = _touch(used, Path(run) / AXIS_AUDIT_REL)
    if not path.exists():
        return {}
    audit = pd.read_csv(path)
    if not {'feature_id', 'symbol'} <= set(audit.columns):
        return {}
    return dict(zip(audit.feature_id.astype(str), audit.symbol.astype(str)))


def _annotation_map(run, used):
    path = _touch(used, Path(run) / ANNOTATION_REL)
    if not path.exists():
        return {}
    frame = pd.read_csv(path)
    if not {'feature_id', 'compound_name'} <= set(frame.columns):
        return {}
    mapping = {}
    for _, row in frame.iterrows():
        mapping.setdefault(str(row.feature_id), str(row.compound_name))
    return mapping


def _feature_label(omics, feature_id, symbols, annotations):
    feature_id = str(feature_id)
    if omics == 'transcriptomics':
        symbol = symbols.get(feature_id, feature_id)
        name = f'{symbol} ({feature_id})' if symbol != feature_id else feature_id
        prefix = 'RNA'
    elif omics == 'proteomics':
        name, prefix = feature_id, 'PROT'
    else:
        compound = annotations.get(feature_id, '')
        name = f'{feature_id} ({compound})' if compound else feature_id
        prefix = 'MET'
    return f'{prefix} · {name}'


def resolve_target(run, identifier):
    """Map a target identifier onto measured feature ids per omics (no selection, all hits kept)."""
    resolution, reasons = {}, []
    audit = Path(run) / AXIS_AUDIT_REL
    if audit.exists():
        frame = pd.read_csv(audit)
        if {'feature_id', 'symbol'} <= set(frame.columns):
            frame = frame[frame.symbol.astype(str).str.upper() == str(identifier).upper()]
            if 'selected_representative' in frame.columns:
                frame = frame[frame.selected_representative.fillna(False).astype(bool)]
            hits = sorted(set(frame.feature_id.astype(str)))
            if hits:
                resolution['transcriptomics'] = hits
            else:
                reasons.append(f'transcriptomics: symbol {identifier!r} not resolved in the frozen mapping audit')
        else:
            reasons.append('transcriptomics: gene-mapping audit lacks feature_id/symbol columns')
    else:
        reasons.append('transcriptomics: frozen gene-mapping audit missing')

    hits = set()
    for cohort in RNA_COHORTS:
        path = Path(run) / 'proteomics' / cohort / 'de.csv'
        if path.exists():
            frame = pd.read_csv(path, usecols=['feature_id'])
            hits |= set(frame.loc[frame.feature_id.astype(str) == str(identifier), 'feature_id'].astype(str))
    if hits:
        resolution['proteomics'] = sorted(hits)
    else:
        reasons.append(f'proteomics: feature_id {identifier!r} not measured')

    hits = set()
    annotation = Path(run) / ANNOTATION_REL
    if annotation.exists():
        frame = pd.read_csv(annotation)
        mask = pd.Series(False, index=frame.index)
        for column in ['feature_id', 'compound_id', 'compound_name']:
            if column in frame.columns:
                mask |= frame[column].astype(str).str.upper().eq(str(identifier).upper())
        hits |= set(frame.loc[mask, 'feature_id'].astype(str))
    for cohort in MET_COHORTS:
        path = Path(run) / 'metabolomics' / cohort / 'de.csv'
        if path.exists():
            frame = pd.read_csv(path, usecols=['feature_id'])
            hits |= set(frame.loc[frame.feature_id.astype(str) == str(identifier), 'feature_id'].astype(str))
    if hits:
        resolution['metabolomics'] = sorted(hits)
    else:
        reasons.append(f'metabolomics: feature id or compound {identifier!r} not measured')
    return resolution, reasons


def target_specs(root, run):
    payload = load_targets(run)
    if not payload or not payload.get('groups'):
        return []
    axes = {axis['axis']: axis for axis in load_axes(root)}
    used, specs = set(), []
    by_identifier = {}
    for group in payload['groups']:
        group_name = str(group.get('group', ''))
        group_type = str(group.get('group_type', 'axis'))
        for target in group.get('targets', []):
            identifier = str(target.get('identifier'))
            cited = [str(e) for e in target.get('evidence_ids', [])]
            if identifier in by_identifier:
                spec = by_identifier[identifier]
                spec['groups'].append(group_name)
                for evidence in cited:
                    if evidence not in spec['evidence_ids_cited']:
                        spec['evidence_ids_cited'].append(evidence)
                continue
            slug = _slugify(identifier, used)
            resolution, reasons = resolve_target(run, identifier)
            spec = {'identifier': identifier, 'slug': slug, 'kind': str(target.get('kind', '')),
                    'group': group_name, 'groups': [group_name], 'group_type': group_type,
                    'evidence_ids_cited': cited, 'resolution': resolution,
                    'unresolved_reasons': reasons,
                    'rationale': target.get('rationale', ''),
                    'validation_approach': target.get('validation_approach', ''),
                    'caveats': target.get('caveats', ''),
                    'axis_definition': axes.get(group_name, {})}
            by_identifier[identifier] = spec
            specs.append(spec)
    return specs


def feature_pairs(resolution):
    pairs = set()
    for omics, ids in resolution.items():
        for feature in ids:
            pairs.add((str(omics), str(feature)))
    return pairs


def spec_pairs(spec, registry):
    """Resolved features plus the features that carry the target's cited evidence (never drop cited rows)."""
    pairs = feature_pairs(spec['resolution'])
    if 'evidence_id' in registry.columns:
        cited = registry[registry.evidence_id.astype(str).isin([str(e) for e in spec.get('evidence_ids_cited', [])])]
        for omics, feature in zip(cited.omics.astype(str), cited.feature_id.astype(str)):
            pairs.add((omics, feature))
    return pairs


def _pairs_mask(frame, pairs):
    if not len(frame):
        return pd.Series(False, index=frame.index)
    keys = list(zip(frame.omics.astype(str), frame.feature_id.astype(str)))
    return pd.Series([key in pairs for key in keys], index=frame.index)


def target_rows(registry, spec):
    return _order_rows(registry[_pairs_mask(registry, spec_pairs(spec, registry))].copy())


def cited_rows(registry, spec):
    frame = registry[registry.evidence_id.astype(str).isin(spec['evidence_ids_cited'])].copy()
    order = {evidence: index for index, evidence in enumerate(spec['evidence_ids_cited'])}
    frame['_order'] = frame.evidence_id.astype(str).map(order)
    return frame.sort_values('_order', kind='mergesort').drop(columns=['_order'])


# --------------------------------------------------------------------------- patient-level data
def _expression_long(run, omics, cohort, used):
    base = Path(run) / omics / cohort
    expression = _touch(used, base / 'expression.csv')
    metadata = _touch(used, base / 'sample_metadata.csv')
    if not expression.exists() or not metadata.exists():
        return None
    matrix = pd.read_csv(expression, index_col=0)
    matrix.index = matrix.index.astype(str)
    meta = pd.read_csv(metadata)
    if not {'patient_id', 'biospecimen_id', 'tissue'} <= set(meta.columns):
        return None
    if 'in_triple109' in meta.columns:
        meta = meta[meta.in_triple109.map(_truthy)]
    lookup = {str(row.biospecimen_id): (str(row.patient_id), str(row.tissue)) for _, row in meta.iterrows()}
    columns = [str(c) for c in matrix.columns if str(c) in lookup]
    if not columns:
        return None
    long = matrix[columns].T.reset_index().rename(columns={'index': 'biospecimen_id'})
    long = long.melt(id_vars='biospecimen_id', var_name='feature_id', value_name='value')
    long['patient_id'] = long.biospecimen_id.map(lambda b: lookup[str(b)][0])
    long['tissue'] = long.biospecimen_id.map(lambda b: lookup[str(b)][1])
    long['omics'] = omics
    long['value'] = pd.to_numeric(long.value, errors='coerce')
    return long.dropna(subset=['value'])


def _clinical(run, used):
    path = _touch(used, Path(run) / CLINICAL_REL)
    if not path.exists():
        return None
    clinical = pd.read_csv(path)
    if not {'patient_id', 'os_months', 'os_event', 'rfs_months', 'rfs_event'} <= set(clinical.columns):
        return None
    return clinical


def _km_curve(times, events):
    times = np.asarray(times, dtype=float)
    events = np.asarray(events).astype(bool)
    order = np.argsort(times, kind='mergesort')
    times, events = times[order], events[order]
    total = len(times)
    step_x, step_y = [0.0], [1.0]
    survival, at_risk = 1.0, total
    for value in np.unique(times):
        mask = times == value
        deaths = int(events[mask].sum())
        if deaths:
            survival *= (1.0 - deaths / at_risk)
            step_x.append(float(value))
            step_y.append(survival)
        at_risk -= int(mask.sum())
    censored = times[~events]
    return np.asarray(step_x), np.asarray(step_y), censored


# --------------------------------------------------------------------------- figures
def _fig_de_effects(root, spec, registry, symbols, annotations, render_cfg, figures, source, used):
    pairs = spec_pairs(spec, registry)
    frame = registry[_pairs_mask(registry, pairs)].copy()
    if 'evidence_layer' in frame.columns:
        frame = frame[frame.evidence_layer.fillna('de').eq('de')]
    frame = _order_rows(frame)
    tested = frame[frame.effect.notna()].copy()
    if not len(tested):
        return None, 'de_effects', 'no estimated differential-expression rows for the resolved features'
    tested['feature_label'] = [_feature_label(o, f, symbols, annotations)
                               for o, f in zip(tested.omics, tested.feature_id)]
    for column, default in [('ci_low', np.nan), ('ci_high', np.nan), ('selected', False), ('padj', np.nan)]:
        if column not in tested.columns:
            tested[column] = default
    tested['threshold'] = np.where(tested.omics.eq('transcriptomics'), RNA_THRESHOLD, OTHER_THRESHOLD)
    tested['row_label'] = tested.feature_label + ' · ' + tested.cohort_id.astype(str)
    tested = tested.iloc[::-1].reset_index(drop=True)
    import matplotlib.pyplot as plt
    height = max(1.6, 0.34 * len(tested) + 0.9)
    fig, ax = plt.subplots(figsize=(4.6, height), layout='constrained')
    positions = np.arange(len(tested))
    selected = tested.selected.fillna(False).astype(bool).to_numpy()
    for index, row in tested.iterrows():
        low = float(row.ci_low) if pd.notna(row.ci_low) else float(row.effect)
        high = float(row.ci_high) if pd.notna(row.ci_high) else float(row.effect)
        color = '#CD584A' if selected[index] else '#9FA3A8'
        ax.plot([low, high], [index, index], color=color, lw=0.8, zorder=2)
        ax.scatter([float(row.effect)], [index], s=18, zorder=3, color=color,
                   facecolors=color if selected[index] else 'white',
                   edgecolors=color, linewidths=0.8)
    for threshold in (RNA_THRESHOLD, OTHER_THRESHOLD):
        ax.axvline(threshold, color='#B8B8B8', lw=0.6, ls=(0, (3, 2)), zorder=1)
        ax.axvline(-threshold, color='#B8B8B8', lw=0.6, ls=(0, (3, 2)), zorder=1)
    ax.axvline(0, color='black', lw=0.5, zorder=1)
    ax.set_yticks(positions)
    ax.set_yticklabels(tested.row_label, fontsize=6.5)
    ax.set_xlabel('effect (RNA log2FC; protein/metabolite paired difference)')
    ax.set_title(f'{spec["slug"]} - frozen differential-expression effects\n'
                 'filled = selected by frozen thresholds; open = not selected; scales not directly comparable',
                 fontsize=8)
    paths = _save_figure(fig, figures / f'{spec["slug"]}_de_effects', render_cfg['formats'])
    columns = ['omics', 'cohort_id', 'feature_id', 'feature_label', 'effect', 'ci_low', 'ci_high',
               'pvalue', 'padj', 'selected', 'direction', 'n_pairs', 'threshold', 'evidence_id']
    _write_csv(tested[[c for c in columns if c in tested.columns]],
               source / f'{spec["slug"]}_de_effects.csv')
    return paths, 'de_effects', None


def _fig_prognosis_forest(root, spec, registry, symbols, annotations, render_cfg, figures, source, used):
    pairs = spec_pairs(spec, registry)
    frame = registry[_pairs_mask(registry, pairs)].copy()
    if 'evidence_layer' not in frame.columns:
        return None, 'prognosis_forest', 'no prognosis evidence layer in the registry'
    frame = frame[frame.evidence_layer.eq('prognosis')]
    if not len(frame):
        return None, 'prognosis_forest', 'no prognosis rows for the resolved features'
    frame = _order_rows(frame)
    frame['feature_label'] = [_feature_label(o, f, symbols, annotations)
                              for o, f in zip(frame.omics, frame.feature_id)]
    for column, default in [('ci_low', np.nan), ('ci_high', np.nan), ('selected', False), ('padj', np.nan),
                            ('hr', np.nan), ('status', 'not_estimable')]:
        if column not in frame.columns:
            frame[column] = default
    frame['row_label'] = (frame.outcome.astype(str).str.upper() + ' · ' + frame.model_type.astype(str)
                          + ' · ' + frame.cohort_id.astype(str) + ' · ' + frame.feature_label)
    import matplotlib.pyplot as plt
    height = max(1.5, 0.36 * len(frame) + 0.9)
    fig, ax = plt.subplots(figsize=(5.4, height), layout='constrained')
    plotted = frame[frame.status.eq('tested') & frame.hr.notna()]
    values = list(plotted.ci_low.dropna()) + list(plotted.ci_high.dropna()) + list(plotted.hr.dropna())
    for index, row in frame.reset_index(drop=True).iterrows():
        if str(row.status) == 'tested' and pd.notna(row.hr):
            color = '#CD584A' if bool(row.selected) else '#3B75AF'
            low = float(row.ci_low) if pd.notna(row.ci_low) else float(row.hr)
            high = float(row.ci_high) if pd.notna(row.ci_high) else float(row.hr)
            ax.plot([low, high], [index, index], color=color, lw=0.8, zorder=2)
            ax.scatter([float(row.hr)], [index], s=18, color=color,
                       facecolors=color if bool(row.selected) else 'white', edgecolors=color, linewidths=0.8)
        else:
            ax.text(1.0, index, 'NE', fontsize=6, ha='center', va='center', color='#9FA3A8')
    ax.axvline(1.0, color='black', lw=0.6, ls=(0, (3, 2)))
    ax.set_xscale('log')
    if values:
        low = min(min(values), 0.5)
        high = max(max(values), 2.0)
        ax.set_xlim(low * 0.9, high * 1.1)
    ax.set_yticks(np.arange(len(frame)))
    ax.set_yticklabels(frame.row_label, fontsize=6.5)
    ax.set_xlabel('hazard ratio per SD of tumor value (95% CI)')
    ax.set_title(f'{spec["slug"]} - pre-registered candidate-axis prognosis (display of frozen Cox models)\n'
                 'single-cohort observational; NE = not estimable; PH diagnostics recorded, not used to switch models',
                 fontsize=8)
    paths = _save_figure(fig, figures / f'{spec["slug"]}_prognosis_forest', render_cfg['formats'])
    columns = ['cohort', 'cohort_id', 'outcome', 'model_type', 'feature_id', 'feature_label', 'n', 'n_events',
               'hr', 'ci_low', 'ci_high', 'pvalue', 'padj', 'ph_p', 'status', 'selected', 'direction', 'evidence_id']
    _write_csv(frame[[c for c in columns if c in frame.columns]],
               source / f'{spec["slug"]}_prognosis_forest.csv')
    return paths, 'prognosis_forest', None


def _fig_paired(root, run, spec, registry, symbols, annotations, render_cfg, figures, source, used):
    pairs = spec_pairs(spec, registry)
    omics_order = [o for o in ['transcriptomics', 'proteomics', 'metabolomics']
                   if any(om == o for om, _ in pairs)]
    panels = []
    for omics in omics_order:
        long = _expression_long(run, omics, PLOT_COHORT, used)
        if long is None:
            continue
        wanted = sorted({f for om, f in pairs if om == omics} & set(long.feature_id.astype(str)))
        for feature in wanted:
            panels.append((omics, feature, long[long.feature_id.astype(str) == feature]))
    if not panels:
        return None, 'paired_tumor_adjacent', 'no patient-level paired values available for the resolved features'
    import matplotlib.pyplot as plt
    per_page = int(render_cfg['max_panels_per_page'])
    pages = [panels[i:i + per_page] for i in range(0, len(panels), per_page)]
    output_paths, source_paths = [], []
    frame_all = None
    for page_index, page in enumerate(pages, start=1):
        rows = int(np.ceil(len(page) / 2))
        fig, axes = plt.subplots(rows, 2, figsize=(6.0, 2.0 * rows), layout='constrained', squeeze=False)
        for index, (omics, feature, long) in enumerate(page):
            axis = axes[index // 2][index % 2]
            pivot = long.pivot_table(index='patient_id', columns='tissue', values='value', aggfunc='first')
            if {'adjacent', 'tumor'} <= set(pivot.columns):
                for _, pair in pivot.dropna(subset=['adjacent', 'tumor']).iterrows():
                    axis.plot([0, 1], [pair['adjacent'], pair['tumor']], color='#B8B8B8', lw=0.3, alpha=0.45, zorder=1)
            for position, tissue, color in [(0, 'adjacent', '#3B75AF'), (1, 'tumor', '#CD584A')]:
                values = pivot[tissue].dropna() if tissue in pivot else pd.Series(dtype=float)
                axis.scatter(np.full(len(values), position), values, s=6, c=color, alpha=0.75, linewidths=0, zorder=2)
                if len(values):
                    axis.scatter([position], [values.mean()], marker='_', s=70, c='black', zorder=3)
            stats = registry[_pairs_mask(registry, {(omics, feature)})]
            if 'evidence_layer' in stats.columns:
                stats = stats[stats.evidence_layer.fillna('de').eq('de')]
            stats = stats[stats.cohort_id.eq(PLOT_COHORT)]
            effect = f'{_fmt_effect(stats.iloc[0])} / padj {_fmt_p(stats.iloc[0].padj)}' if len(stats) else 'DE row unavailable'
            axis.set_xticks([0, 1])
            axis.set_xticklabels(['adjacent', 'tumor'], fontsize=6.5)
            axis.set_title(f'{_feature_label(omics, feature, symbols, annotations)}  n={pivot.shape[0]}\n{effect}', fontsize=7)
            axis.tick_params(labelsize=6)
            frame_all = pd.concat([frame_all, long.assign(feature_label=_feature_label(omics, feature, symbols, annotations))],
                                  ignore_index=True) if frame_all is not None else long.assign(
                feature_label=_feature_label(omics, feature, symbols, annotations))
        for index in range(len(page), rows * 2):
            axes[index // 2][index % 2].axis('off')
        fig.suptitle('Paired tumor vs adjacent patient values (frozen expression matrices; estimate/padj from the registry)',
                     fontsize=8)
        paths = _save_figure(fig, figures / f'{spec["slug"]}_paired_tumor_adjacent_p{page_index}', render_cfg['formats'])
        output_paths.extend(paths)
        source_path = source / f'{spec["slug"]}_paired_tumor_adjacent_p{page_index}.csv'
        _write_csv(frame_all, source_path)
        source_paths.append(source_path)
    return output_paths, 'paired_tumor_adjacent', None


def _fig_km(root, run, spec, registry, symbols, annotations, render_cfg, figures, source, used):
    clinical = _clinical(run, used)
    if clinical is None:
        return None, 'km_display', 'patient_clinical.csv missing or lacks OS/RFS columns'
    panels = []
    pairs = spec_pairs(spec, registry)
    omics_order = [o for o in ['transcriptomics', 'proteomics', 'metabolomics']
                   if any(om == o for om, _ in pairs)]
    for omics in omics_order:
        long = _expression_long(run, omics, PLOT_COHORT, used)
        if long is None:
            continue
        tumor = long[long.tissue == 'tumor']
        for feature in sorted({f for om, f in pairs if om == omics}):
            values = tumor[tumor.feature_id.astype(str) == feature][['patient_id', 'value']]
            if not len(values):
                continue
            panels.append((omics, feature, values.rename(columns={'value': 'tumor_value'})))
    if not panels:
        return None, 'km_display', 'no tumor-level feature values available for descriptive survival display'
    import matplotlib.pyplot as plt
    outcomes = [o for o in render_cfg['km_outcomes'] if f'{o}_months' in clinical.columns and f'{o}_event' in clinical.columns]
    if not outcomes:
        return None, 'km_display', 'no configured survival outcome available'
    fig, axes = plt.subplots(len(panels), len(outcomes), figsize=(3.2 * len(outcomes), 2.3 * len(panels)),
                             layout='constrained', squeeze=False)
    source_frames, skipped = [], []
    for row_index, (omics, feature, values) in enumerate(panels):
        for col_index, outcome in enumerate(outcomes):
            axis = axes[row_index][col_index]
            merged = values.merge(clinical[['patient_id', f'{outcome}_months', f'{outcome}_event']],
                                  on='patient_id', how='inner').dropna()
            if merged.tumor_value.nunique() < 2 or len(merged) < 4:
                axis.axis('off')
                axis.set_title(f'{feature} - {outcome.upper()}: insufficient values', fontsize=6.5)
                skipped.append({'panel': f'km_display:{feature}:{outcome}', 'reason': 'insufficient tumor values or events'})
                continue
            median = float(merged.tumor_value.median())
            merged['stratum'] = np.where(merged.tumor_value > median, f'above median ({median:.3g})',
                                         f'at/below median ({median:.3g})')
            records = []
            for stratum, group in merged.groupby('stratum', sort=True):
                times = group[f'{outcome}_months'].to_numpy(dtype=float)
                events = group[f'{outcome}_event'].astype(float).to_numpy() > 0
                step_x, step_y, censored = _km_curve(times, events)
                color = '#CD584A' if 'above' in stratum else '#3B75AF'
                axis.step(step_x, step_y, where='post', color=color, lw=1.1,
                          label=f'{stratum}: n={len(group)}, events={int(events.sum())}')
                if len(censored):
                    axis.plot(censored, [step_y[np.searchsorted(step_x, c, side='right') - 1] for c in censored],
                              linestyle='none', marker='|', color=color, ms=3)
                records.append(group.assign(outcome=outcome, stratum=stratum, omics=omics, feature_id=feature))
            axis.set_ylim(0, 1.02)
            axis.set_xlabel(f'{outcome.upper()} months', fontsize=6.5)
            axis.set_ylabel('survival probability', fontsize=6.5)
            axis.tick_params(labelsize=6)
            axis.legend(frameon=False, fontsize=5.5, loc='lower left')
            axis.set_title(f'{_feature_label(omics, feature, symbols, annotations)} - {outcome.upper()}', fontsize=7)
            source_frames.append(pd.concat(records, ignore_index=True))
    if not source_frames:
        plt.close(fig)
        return None, 'km_display', 'no feature had enough tumor values/events for the descriptive KM display'
    fig.suptitle('DISPLAY ONLY: median split of frozen tumor values; no log-rank or any test; not part of the frozen analysis',
                 fontsize=8)
    paths = _save_figure(fig, figures / f'{spec["slug"]}_km_display', render_cfg['formats'])
    frame = pd.concat(source_frames, ignore_index=True)
    frame['feature_label'] = [_feature_label(o, f, symbols, annotations) for o, f in zip(frame.omics, frame.feature_id)]
    _write_csv(frame, source / f'{spec["slug"]}_km_display.csv')
    return paths, 'km_display', None


# --------------------------------------------------------------------------- evidence tables
DE_COLUMNS = ['omics', 'cohort_id', 'feature_id', 'effect', 'se', 'pvalue', 'padj', 'ci_low', 'ci_high',
              'test_status', 'converged', 'selected', 'direction', 'n_pairs', 'effect_scale', 'method',
              'evidence_id', 'artifact_path']
PROGNOSIS_COLUMNS = ['cohort', 'cohort_id', 'outcome', 'model_type', 'feature_id', 'entity', 'omics',
                     'n', 'n_events', 'hr', 'ci_low', 'ci_high', 'pvalue', 'padj', 'ph_p', 'status',
                     'effect_scale', 'selected', 'direction', 'evidence_id']
TCELL_COLUMNS = ['readout_kind', 'readout_panel', 'readout', 'feature_id', 'entity', 'omics', 'n_pairs',
                 'rho', 'ci_low', 'ci_high', 'ci_method', 'pvalue', 'padj', 'selected', 'direction',
                 'limitation', 'claim_type', 'evidence_id']


def _evidence_tables(spec, registry, evidence_dir):
    rows = target_rows(registry, spec)
    cited = cited_rows(registry, spec)
    _write_csv(cited, evidence_dir / 'evidence_cited.csv')
    _write_csv(rows, evidence_dir / 'evidence_all_target.csv')
    de = rows[rows.evidence_layer.fillna('de').eq('de')] if 'evidence_layer' in rows.columns else rows
    _write_csv(de[[c for c in DE_COLUMNS if c in de.columns]], evidence_dir / 'de_by_omics.csv')
    if 'evidence_layer' in rows.columns:
        prognosis = rows[rows.evidence_layer.eq('prognosis')]
        if len(prognosis):
            _write_csv(prognosis[[c for c in PROGNOSIS_COLUMNS if c in prognosis.columns]],
                       evidence_dir / 'prognosis_models.csv')
        tcell = rows[rows.evidence_layer.eq('tcell_correlation')]
        if len(tcell):
            _write_csv(tcell[[c for c in TCELL_COLUMNS if c in tcell.columns]],
                       evidence_dir / 'tcell_correlations.csv')
    return rows, cited


# --------------------------------------------------------------------------- text sections
def _boundaries(rows):
    layers = sorted(set(str(layer) for layer in rows['evidence_layer'].fillna('de'))) \
        if 'evidence_layer' in rows.columns else ['de']
    return {'validation_status': 'not_independently_validated', 'evidence_layers': layers,
            'prognosis': BOUNDARY_PROGNOSIS, 'tcell': BOUNDARY_TCell, 'km': BOUNDARY_KM,
            'cohort_nesting': BOUNDARY_NESTING}


def _validation_plan(root, spec, used):
    path = Path(root) / HYPOTHESES_REL
    if not path.exists():
        return ['未找到假说文件；本靶点没有对应的模型验证计划。', '']
    _touch(used, path)
    payload = _read_json(path)
    hypothesis = None
    for item in payload.get('hypotheses', []):
        if str(item.get('axis')) in set(spec['groups']) or str(item.get('axis')) == spec['group']:
            hypothesis = item
            break
    if hypothesis is None:
        return [f'本次运行未产生与该组（{spec["group"]}）对应的证据锚定型假说，因此没有模型生成的验证计划。',
                '靶点本身仍按冻结证据完整登记；不得据此声称机制或验证状态。', '']
    lines = [f'- 关联假说：`{hypothesis.get("id")}` {hypothesis.get("title", "")}（轴：{hypothesis.get("axis")}）',
             f'- 假说陈述（模型生成）：{hypothesis.get("statement", "")}', '- 可证伪预测：']
    for prediction in hypothesis.get('testable_predictions', []):
        lines.append(f'  - 预测：{prediction.get("prediction")}；读数：{prediction.get("readout")}；'
                     f'若成立：{prediction.get("expected_if_true")}；若不成立：{prediction.get("expected_if_false")}')
    lines.append('- 验证实验：')
    for experiment in hypothesis.get('validation_experiments', []):
        lines.append(f'  - 体系：{experiment.get("model_system")}；扰动：{experiment.get("perturbation")}；'
                     f'对照：{experiment.get("controls")}；读数：{experiment.get("readout")}；'
                     f'可行性：{experiment.get("feasibility")}。{experiment.get("notes", "")}')
    lines.append('- 替代解释：')
    lines += [f'  - {item}' for item in hypothesis.get('alternative_explanations', [])] or ['  - 未列出']
    lines.append('- 未知与风险：')
    lines += [f'  - {item}' for item in hypothesis.get('unknowns', [])] or ['  - 未列出']
    lines += ['', '> 以上为**模型生成·待人工复核**内容，不是证据；验证状态不因本证据包而改变。', '']
    return lines


def _readme(spec, rows, cited, manifest, render_cfg, skipped, symbols, annotations):
    lines = [f'# 靶点证据包：{spec["identifier"]}', '',
             f'- 目录：`result/{spec["slug"]}/`（来源运行 `{manifest["source_run"]["run_id"]}`）',
             f'- 分组：{spec["group"]}（{spec["group_type"]}）；模型标注类型：{spec["kind"] or "未标注"}',
             f'- 解析到的实测特征：RNA {spec["resolution"].get("transcriptomics", [])}；'
             f'蛋白 {spec["resolution"].get("proteomics", [])}；代谢 {spec["resolution"].get("metabolomics", [])}']
    if len(cited):
        cited_features = sorted({(str(o), str(f)) for o, f in zip(cited.omics.astype(str), cited.feature_id.astype(str))})
        lines.append('- 引用证据携带的特征（同一并纳入表与图，避免丢失被引用的峰/基因）：'
                     + '; '.join(f'{o}:{f}' for o, f in cited_features))
    lines.append(f'- 证据状态：`validation_status={manifest["boundaries"]["validation_status"]}`；'
                 '本证据包只汇总冻结证据，不新增检验、不排序、不提升结论强度。')
    lines.append('')
    if spec.get('unresolved_reasons'):
        lines += ['## 未测到 / 未解析的组学层（如实记录，不影响其他已解析层）', '']
        lines += [f'- {reason}' for reason in spec['unresolved_reasons']]
        lines += ['']
    lines += ['## 引用证据（数值来自证据注册表）', '']
    if len(cited):
        header = ['evidence_id', '层', '组学', '队列', '特征', '结局/读数', '效应', 'padj', 'selected', '方向']
        body = []
        for _, row in cited.iterrows():
            layer = str(row.get('evidence_layer') or 'de')
            if layer == 'prognosis':
                readout = str(row.get('outcome', '')).upper()
            elif layer == 'tcell_correlation':
                readout = str(row.get('readout', ''))
                panel = row.get('readout_panel')
                if pd.notna(panel):
                    readout = f'{readout} ({panel})'
            else:
                readout = ''
            name = symbols.get(str(row.feature_id), str(row.feature_id)) \
                if str(row.omics) == 'transcriptomics' else str(row.feature_id)
            body.append([f'`{row.evidence_id}`', layer, row.omics, _cohort_value(row),
                         name, readout, _fmt_effect(row), _fmt_p(row.get('padj')),
                         bool(row.get('selected')) if pd.notna(row.get('selected')) else 'NA', row.get('direction', '')])
        lines += _md_table(header, body)
    else:
        lines.append('该靶点在本次运行中没有引用的证据 ID。')
    lines += ['', '## 同靶点其他证据（含未入选、not-estimable 与方向相反）', '',
              f'- 注册表中该靶点全部特征相关行：{len(rows)} 行（完整表见 `evidence/evidence_all_target.csv`）。']
    if 'evidence_layer' in rows.columns:
        for layer, group in rows.groupby(rows.evidence_layer.fillna('de')):
            selected = int(group.selected.fillna(False).astype(bool).sum()) if 'selected' in group.columns else 0
            not_estimable = int(group.status.eq('not_estimable').sum()) if 'status' in group.columns else 0
            lines.append(f'  - `{layer}`：{len(group)} 行（selected {selected}；not-estimable {not_estimable}）。')
    lines += ['', '## 读数边界（固定限制）', '',
              '- 预后：单队列观察性 Cox（肿瘤值每 SD、95% CI；RFS 未使用竞争风险模型；PH 诊断仅记录不切换模型）。',
              '- T 细胞读数：外部图谱 NNLS 估计比例或 RNA 同源 ssGSEA 评分，不是实测细胞比例，也不构成独立证据层。',
              '- K-M 显示（若存在）：中位分组仅为描述性展示，未做 log-rank 或任何检验，不属于冻结分析。',
              '- 队列关系：109 人是 159 人的嵌套子集，两者不构成发现/独立验证关系；观察性配对 bulk 组织，不建立因果、酶活性、通量或细胞来源。',
              '- 3MT 相关峰身份仍有歧义（MetDNA grade 不是 MSI 等级）；代谢上游处理历史未知。',
              '- 文献立场为机器评估/检索匹配（`stance=unreviewed`），人工复核前不得作为支持或反对证据。', '']
    if skipped:
        lines += ['## 跳过的面板（数据不足；如实记录）', '']
        lines += [f'- {item["panel"]}：{item["reason"]}"' for item in skipped]
        lines += ['']
    lines += ['## 复现与校验', '',
              '从项目根目录执行（要求该 run 的冻结产物仍然存在）：', '',
              '```bash',
              f'bash result/{spec["slug"]}/code/reproduce.sh          # 重渲染并与 manifest 逐字节比对',
              f'bash scripts/00_run.sh verify-dossiers --run-id {manifest["source_run"]["run_id"]}',
              '```', '',
              '- `source_data/` 含去标识患者级数值（沿用项目既有图源表惯例），外发前需复核。',
              '- 本包的内容哈希、输入哈希与生成器哈希记录在 `manifest.json`；校验命令见上。', '']
    return '\n'.join(lines)


def _reproduce_script(slug, run_id):
    return ('#!/usr/bin/env bash\n'
            'set -euo pipefail\n'
            'cd "$(dirname "$0")/../../.."\n'
            f'.venv/bin/python result/{slug}/code/dossiers.py reproduce --project-root . '
            f'--run-id {run_id} --target {slug} --dossier result/{slug}\n')


# --------------------------------------------------------------------------- render one target
def _render_target(root, run, spec, render_cfg, source_run, out_dir):
    """Render all dossier files except manifest.json; returns (outputs, inputs, skipped)."""
    root, run, out_dir = Path(root), Path(run), Path(out_dir)
    used, skipped = set(), []
    registry = load_registry(run)
    _touch(used, run / REGISTRY_REL)
    _touch(used, run / TARGETS_REL)
    symbols = _symbol_map(run, used)
    annotations = _annotation_map(run, used)
    evidence_dir = out_dir / 'evidence'
    figures = out_dir / 'figures'
    source = out_dir / 'source_data'
    evidence_dir.mkdir(parents=True, exist_ok=True)
    figures.mkdir(parents=True, exist_ok=True)
    source.mkdir(parents=True, exist_ok=True)

    rows, cited = _evidence_tables(spec, registry, evidence_dir)
    panels = list(render_cfg['panels'])
    renderers = {'de_effects': lambda: _fig_de_effects(root, spec, registry, symbols, annotations, render_cfg, figures, source, used),
                 'prognosis_forest': lambda: _fig_prognosis_forest(root, spec, registry, symbols, annotations, render_cfg, figures, source, used),
                 'paired_tumor_adjacent': lambda: _fig_paired(root, run, spec, registry, symbols, annotations, render_cfg, figures, source, used),
                 'km_display': lambda: _fig_km(root, run, spec, registry, symbols, annotations, render_cfg, figures, source, used)}
    for panel in panels:
        if panel not in renderers:
            skipped.append({'panel': panel, 'reason': 'unknown panel name in deliverables.dossiers.panels'})
            continue
        paths, name, reason = renderers[panel]()
        if paths is None:
            skipped.append({'panel': name, 'reason': reason})

    if render_cfg['include_literature_table']:
        literature = Path(run) / LITERATURE_REL
        if literature.exists():
            _touch(used, literature)
            payload = _read_json(literature)
            records = [r for r in payload.get('records', []) if str(r.get('axis')) in set(spec['groups'])]
            if records:
                frame = pd.DataFrame([{'pmid': r.get('pmid'), 'axis': r.get('axis'), 'title': r.get('title'),
                                       'retrieval_source': r.get('retrieval_source'),
                                       'url': r.get('url'), 'stance': 'unreviewed'} for r in records])
                _write_csv(frame, evidence_dir / 'literature_axis.csv')

    plan_lines = _validation_plan(root, spec, used) if render_cfg['include_model_validation_plan'] else ['未启用模型验证计划。', '']
    _write_text(out_dir / 'validation_plan.md',
                '\n'.join([f'# {spec["identifier"]} 待验证计划（模型生成·待人工复核）', ''] + plan_lines))

    manifest_stub = {'source_run': source_run, 'boundaries': _boundaries(rows)}
    readme = _readme(spec, rows, cited, manifest_stub, render_cfg, skipped, symbols, annotations)
    _write_text(out_dir / 'README.md', readme)

    code_source = Path(__file__).resolve()
    _write_text(out_dir / CODE_COPY, code_source.read_text(encoding='utf-8'))
    _write_text(out_dir / REPRODUCE_SCRIPT, _reproduce_script(spec['slug'], source_run['run_id']))
    os.chmod(out_dir / REPRODUCE_SCRIPT, 0o755)

    outputs = sorted([path for path in out_dir.rglob('*') if path.is_file()], key=lambda p: p.relative_to(out_dir).as_posix())
    return [path.relative_to(out_dir).as_posix() for path in outputs], sorted(used), skipped


def _content_hash(out_dir, outputs):
    digest = hashlib.sha256()
    for relative in sorted(outputs):
        digest.update(relative.encode())
        digest.update(b'\0')
        digest.update(_sha256(Path(out_dir) / relative).encode())
        digest.update(b'\n')
    return digest.hexdigest()


def _validate_manifest(root, manifest):
    schema_path = Path(root) / 'schemas/dossier_manifest.schema.json'
    if not schema_path.exists():
        return
    import jsonschema
    jsonschema.Draft7Validator(_read_json(schema_path)).validate(manifest)


def build_one(root, run, spec, render_cfg, source_run, force=False):
    run = Path(run)
    target_root = run / 'target_dossiers'
    target_root.mkdir(parents=True, exist_ok=True)
    out_dir = target_root / spec['slug']
    temp = target_root / f'.tmp-{spec["slug"]}'
    if temp.exists():
        shutil.rmtree(temp)
    temp.mkdir(parents=True)
    outputs, inputs, skipped = _render_target(root, run, spec, render_cfg, source_run, temp)
    registry = load_registry(run)
    rows = target_rows(registry, spec)
    manifest = {'schema_version': SCHEMA_VERSION,
                'target': {'identifier': spec['identifier'], 'slug': spec['slug'], 'kind': spec['kind'],
                           'group': spec['group'], 'group_type': spec['group_type'],
                           'evidence_ids_cited': spec['evidence_ids_cited']},
                'source_run': source_run,
                'resolution': {**{k: v for k, v in spec['resolution'].items()},
                               'unresolved_reasons': spec['unresolved_reasons']},
                'boundaries': _boundaries(rows),
                'render_config': render_cfg,
                'inputs': [{'path': str(path.relative_to(run).as_posix()), 'sha256': _sha256(path)} for path in inputs],
                'code': {'module': 'orchestrator/dossiers.py', 'sha256': _sha256(Path(__file__).resolve())},
                'outputs': [{'path': relative, 'sha256': _sha256(temp / relative),
                             'bytes': (temp / relative).stat().st_size} for relative in outputs],
                'content_hash': _content_hash(temp, outputs),
                'generated_at': _now(),
                'status': 'complete' if spec['resolution'] else 'partial',
                'skipped_panels': skipped}
    _write_json(temp / MANIFEST_NAME, manifest)
    _validate_manifest(root, manifest)
    # Replace the previous build of this target atomically (derived output inside this run).
    if out_dir.exists():
        LOG.info('Replacing previous dossier build at %s', out_dir)
        shutil.rmtree(out_dir)
    os.replace(temp, out_dir)
    return manifest


def build_target_dossiers(root, run, cfg=None):
    """Deterministic stage: build one in-run dossier per proposed target (no LLM, no new statistics)."""
    root, run = Path(root), Path(run)
    cfg = cfg or _load_config(root)
    block = ((cfg or {}).get('deliverables', {}) or {}).get('dossiers', {}) or {}
    if block.get('enabled') is False:
        return {'status': 'disabled', 'n_targets': 0, 'targets': [], 'errors': []}
    try:
        specs = target_specs(root, run)
    except Exception as exc:
        LOG.exception('Target dossier resolution failed')
        return {'status': 'failed', 'n_targets': 0, 'targets': [], 'errors': [{'target': None, 'message': str(exc)}]}
    if not specs:
        return {'status': 'skipped_no_targets', 'n_targets': 0, 'targets': [], 'errors': []}
    render_cfg = render_settings(cfg)
    run_json = Path(run) / 'run.json'
    artifact_manifest = Path(run) / 'artifact_manifest.json'
    source_run = {'run_id': run.name,
                  'fingerprint': _read_json(run_json).get('fingerprint', '') if run_json.exists() else '',
                  'artifact_manifest_sha256': _sha256(artifact_manifest) if artifact_manifest.exists() else ''}
    targets, errors = [], []
    registry = None
    try:
        registry = load_registry(run)
    except Exception:
        registry = None
    for spec in specs:
        try:
            if registry is not None:
                known = set(registry.evidence_id.astype(str))
                unknown = [e for e in spec['evidence_ids_cited'] if e not in known]
                if unknown:
                    raise ValueError(f'cites unknown evidence ids: {unknown[:5]}')
            manifest = build_one(root, run, spec, render_cfg, source_run)
            entry = {'slug': spec['slug'], 'identifier': spec['identifier'], 'groups': spec['groups'],
                     'status': manifest['status'], 'content_hash': manifest['content_hash'],
                     'n_evidence_cited': len(spec['evidence_ids_cited']),
                     'n_skipped_panels': len(manifest['skipped_panels'])}
        except Exception as exc:
            LOG.exception('Target dossier failed for %s', spec['identifier'])
            errors.append({'target': spec['identifier'], 'message': str(exc)})
            entry = {'slug': spec['slug'], 'identifier': spec['identifier'], 'groups': spec['groups'],
                     'status': 'failed', 'error': str(exc)}
        targets.append(entry)
    status = 'complete' if targets and all(t['status'] in {'complete', 'partial'} for t in targets) else 'failed'
    summary = {'status': status, 'n_targets': len(targets), 'output_dir': str(block.get('output_dir') or 'result'),
               'targets': targets, 'errors': errors, 'build_dir': 'target_dossiers',
               'authority': 'deterministic_aggregation_of_frozen_evidence_no_new_statistics'}
    _write_json(run / 'report/target_dossiers.json', summary)
    LOG.info('Target dossiers built: %s', {t['identifier']: t['status'] for t in targets})
    return summary


# --------------------------------------------------------------------------- publish / verify
def _copy_tree(source, destination):
    shutil.copytree(source, destination)


def _same_content(left, right):
    left, right = Path(left), Path(right)
    left_files = {p.relative_to(left).as_posix(): _sha256(p) for p in left.rglob('*') if p.is_file()}
    right_files = {p.relative_to(right).as_posix(): _sha256(p) for p in right.rglob('*') if p.is_file()}
    return left_files == right_files


def _place(root, run, slug, destination, force, output_dir):
    source = Path(run) / 'target_dossiers' / slug
    manifest = _read_json(source / MANIFEST_NAME)
    if not destination.exists():
        temp = Path(output_dir) / f'.tmp-{slug}-{os.getpid()}'
        if temp.exists():
            shutil.rmtree(temp)
        _copy_tree(source, temp)
        os.replace(temp, destination)
        return 'published', manifest
    existing_manifest = destination / MANIFEST_NAME
    if not existing_manifest.exists():
        if not force:
            raise ValueError(f'{destination} exists without a dossier manifest; re-run with --force to replace it')
        backup = Path(output_dir) / f'.replaced-{datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")}-{slug}'
        os.replace(destination, backup)
        LOG.warning('Existing non-dossier directory backed up to %s', backup)
        return _place(root, run, slug, destination, False, output_dir)
    other = _read_json(existing_manifest)
    if other.get('content_hash') == manifest['content_hash'] and _same_content(source, destination):
        return 'unchanged', manifest
    same_run = other.get('source_run', {}).get('run_id') == manifest.get('source_run', {}).get('run_id')
    if not same_run and not force:
        raise ValueError(f'Conflicting dossier at {destination}: published from run '
                         f'{other.get("source_run", {}).get("run_id")!r}, current build is from '
                         f'{manifest.get("source_run", {}).get("run_id")!r}; use --force to replace')
    if same_run:
        # Re-render of the same run's own derived output: replace in place, no backup clutter.
        shutil.rmtree(destination)
    else:
        backup = Path(output_dir) / f'.replaced-{datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")}-{slug}'
        os.replace(destination, backup)
        LOG.warning('Previous dossier backed up to %s', backup)
    temp = Path(output_dir) / f'.tmp-{slug}-{os.getpid()}'
    if temp.exists():
        shutil.rmtree(temp)
    _copy_tree(source, temp)
    os.replace(temp, destination)
    return 'replaced', manifest


def _index_readme(output_dir, index):
    lines = ['# 靶点证据包索引', '',
             f"- 来源运行：`{index['source_run_id']}`",
             f"- 靶点数：{len(index['targets'])}",
             '- 每个子目录为该靶点的可复现证据包（README、manifest、证据表、证据图与 source data、复现代码）。',
             '- 证据包只汇总冻结证据：不新增统计检验、不排序、不改变验证状态（`not_independently_validated`）。',
             '- K-M 面板仅中位分组描述性展示（无 log-rank 或任何检验）；T 细胞读数为估计/RNA 同源读数。', '',
             '## 靶点列表', '']
    header = ['目录', '标识', '分组', '状态', '引用证据数', '跳过面板数']
    body = [[f'`{slug}/`', item.get('identifier'), ';'.join(item.get('groups', [])), item.get('status'),
             item.get('n_evidence_cited', 'NA'), item.get('n_skipped_panels', 'NA')]
            for slug, item in sorted(index['targets'].items())]
    lines += _md_table(header, body)
    lines += ['', '## 校验', '', '```bash',
              f'bash scripts/00_run.sh verify-dossiers --run-id {index["source_run_id"]}',
              '```', '']
    return '\n'.join(lines)


def publish_dossiers(root, run, cfg=None, force=False):
    """Publish built dossiers to the project-root result directory with idempotent guards."""
    root, run = Path(root), Path(run)
    cfg = cfg or _load_config(root)
    output_dir = output_root(root, cfg)
    source_root = Path(run) / 'target_dossiers'
    manifests = sorted(source_root.glob(f'*/{MANIFEST_NAME}')) if source_root.exists() else []
    if not manifests:
        return {'status': 'skipped_no_build', 'n_published': 0, 'n_unchanged': 0, 'n_failed': 0, 'targets': []}
    output_dir.mkdir(parents=True, exist_ok=True)
    index_path = output_dir / '_index.json'
    index = _read_json(index_path) if index_path.exists() else {'schema_version': SCHEMA_VERSION, 'targets': {}}
    index['targets'] = {}
    published = unchanged = replaced = 0
    failures = []
    for manifest_path in manifests:
        slug = manifest_path.parent.name
        destination = output_dir / slug
        try:
            action, manifest = _place(root, run, slug, destination, force, output_dir)
            published += int(action == 'published')
            unchanged += int(action == 'unchanged')
            replaced += int(action == 'replaced')
            index['targets'][slug] = {'identifier': manifest['target']['identifier'],
                                      'groups': [manifest['target'].get('group', '')],
                                      'status': manifest['status'],
                                      'content_hash': manifest['content_hash'],
                                      'manifest_sha256': _sha256(destination / MANIFEST_NAME),
                                      'n_evidence_cited': len(manifest['target']['evidence_ids_cited']),
                                      'n_skipped_panels': len(manifest.get('skipped_panels', [])),
                                      'published_at': _now(), 'action': action}
        except Exception as exc:
            LOG.exception('Publishing dossier failed for %s', slug)
            failures.append({'slug': slug, 'error': str(exc)})
    index['source_run_id'] = manifests[0].parent.parent.parent.name
    index['schema_version'] = SCHEMA_VERSION
    index['n_published'] = published
    index['n_unchanged'] = unchanged
    index['n_replaced'] = replaced
    index['n_failed'] = len(failures)
    index['failures'] = failures
    index['status'] = 'complete' if not failures else 'partial'
    _write_json(index_path, index)
    _write_text(output_dir / 'README.md', _index_readme(output_dir, index))
    summary = {'status': index['status'], 'output_dir': str(output_dir), 'n_published': published,
               'n_unchanged': unchanged, 'n_replaced': replaced, 'n_failed': len(failures),
               'failures': failures, 'targets': index['targets']}
    report_path = Path(run) / 'report/target_dossiers.json'
    if report_path.exists():
        report = _read_json(report_path)
        report['publish'] = {k: summary[k] for k in ['status', 'output_dir', 'n_published', 'n_unchanged',
                                                     'n_replaced', 'n_failed', 'failures']}
        _write_json(report_path, report)
    if failures:
        # A publish conflict is an actionable error, not a silent partial: the index keeps the
        # per-target detail and the caller decides (runner degrades the run to partial; the
        # standalone command reports the conflict and instructs --force).
        raise ValueError('Dossier publication failed: ' + '; '.join(f'{f["slug"]}: {f["error"]}' for f in failures))
    LOG.info('Dossiers published to %s: %s', output_dir, summary['status'])
    return summary


def _verify_target(root, run, destination, rerender):
    problems = []
    manifest = _read_json(Path(destination) / MANIFEST_NAME)
    for item in manifest['outputs']:
        path = Path(destination) / item['path']
        if not path.is_file():
            problems.append(f'{destination.name}/{item["path"]}: missing')
        elif _sha256(path) != item['sha256'] or path.stat().st_size != item['bytes']:
            problems.append(f'{destination.name}/{item["path"]}: hash mismatch')
    recomputed = _content_hash(destination, [item['path'] for item in manifest['outputs']])
    if recomputed != manifest['content_hash']:
        problems.append(f'{destination.name}: content hash mismatch ({recomputed} != {manifest["content_hash"]})')
    for item in manifest['inputs']:
        path = Path(run) / item['path']
        if not path.exists():
            problems.append(f'{destination.name}: input missing {item["path"]}')
        elif _sha256(path) != item['sha256']:
            problems.append(f'{destination.name}: input changed {item["path"]}')
    code_path = Path(destination) / CODE_COPY
    if not code_path.exists() or _sha256(code_path) != manifest['code']['sha256']:
        problems.append(f'{destination.name}: dossier code copy does not match the manifest hash')
    built = Path(run) / 'target_dossiers' / destination.name
    if built.exists() and not _same_content(built, destination):
        problems.append(f'{destination.name}: published copy differs from the in-run build')
    if rerender:
        spec = {'identifier': manifest['target']['identifier'], 'slug': manifest['target']['slug'],
                'kind': manifest['target']['kind'], 'group': manifest['target']['group'],
                'groups': [manifest['target']['group']], 'group_type': manifest['target']['group_type'],
                'evidence_ids_cited': manifest['target']['evidence_ids_cited'],
                'resolution': {k: v for k, v in manifest['resolution'].items() if k != 'unresolved_reasons'},
                'unresolved_reasons': manifest['resolution'].get('unresolved_reasons', [])}
        with tempfile.TemporaryDirectory() as temp:
            outputs, _, _ = _render_target(root, run, spec, manifest['render_config'],
                                           manifest['source_run'], temp)
            if sorted(outputs) != sorted(item['path'] for item in manifest['outputs']):
                problems.append(f'{destination.name}: re-render file set differs')
            else:
                for relative in outputs:
                    if _sha256(Path(temp) / relative) != _sha256(Path(destination) / relative):
                        problems.append(f'{destination.name}: re-render not byte-identical for {relative}')
    return problems, manifest


def verify_dossiers(root, run, cfg=None, rerender=True):
    """Verify published dossiers: file hashes, input integrity, in-run copy equality and re-render."""
    root, run = Path(root), Path(run)
    cfg = cfg or _load_config(root)
    output_dir = output_root(root, cfg)
    index_path = output_dir / '_index.json'
    if not index_path.exists():
        raise ValueError(f'No dossier index at {index_path}; run "dossiers" first')
    index = _read_json(index_path)
    problems = []
    for slug in sorted(index.get('targets', {})):
        destination = output_dir / slug
        if not destination.exists():
            problems.append(f'{slug}: published directory missing')
            continue
        target_problems, _ = _verify_target(root, run, destination, rerender)
        problems.extend(target_problems)
    if problems:
        raise ValueError('Dossier verification failed: ' + '; '.join(problems[:10]))
    return {'status': 'verified', 'n_targets': len(index.get('targets', {})),
            'rerender': bool(rerender), 'source_run_id': index.get('source_run_id')}


# --------------------------------------------------------------------------- CLI
def _reproduce(root, run, target_slug, dossier, in_place):
    dossier = Path(dossier)
    manifest = _read_json(dossier / MANIFEST_NAME)
    if manifest['target']['slug'] != target_slug:
        raise ValueError(f'Dossier {dossier} is for {manifest["target"]["slug"]!r}, not {target_slug!r}')
    if manifest['source_run']['run_id'] != Path(run).name:
        raise ValueError(f'Dossier was built from run {manifest["source_run"]["run_id"]!r}, not {Path(run).name!r}')
    for item in manifest['inputs']:
        path = Path(run) / item['path']
        if not path.exists() or _sha256(path) != item['sha256']:
            raise ValueError(f'Frozen input changed or missing: {item["path"]}')
    if _sha256(Path(__file__).resolve()) != manifest['code']['sha256']:
        raise ValueError('This generator copy does not match the manifest code hash; use the recorded copy verbatim')
    spec = {'identifier': manifest['target']['identifier'], 'slug': manifest['target']['slug'],
            'kind': manifest['target']['kind'], 'group': manifest['target']['group'],
            'groups': [manifest['target']['group']], 'group_type': manifest['target']['group_type'],
            'evidence_ids_cited': manifest['target']['evidence_ids_cited'],
            'resolution': {k: v for k, v in manifest['resolution'].items() if k != 'unresolved_reasons'},
            'unresolved_reasons': manifest['resolution'].get('unresolved_reasons', [])}
    with tempfile.TemporaryDirectory() as temp:
        outputs, _, _ = _render_target(root, run, spec, manifest['render_config'], manifest['source_run'], temp)
        expected = {item['path']: item['sha256'] for item in manifest['outputs']}
        if sorted(outputs) != sorted(expected):
            raise ValueError('Re-render produced a different file set than the manifest')
        mismatches = [relative for relative in outputs if _sha256(Path(temp) / relative) != expected[relative]]
        if mismatches:
            raise ValueError(f'Re-render is not byte-identical for: {mismatches[:5]}')
        if in_place:
            for relative in outputs:
                shutil.copyfile(Path(temp) / relative, dossier / relative)
    return {'status': 'reproduced', 'slug': target_slug, 'n_outputs': len(outputs)}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Target dossier stage (frozen evidence aggregation)')
    parser.add_argument('command', choices=['build', 'publish', 'check', 'reproduce'])
    parser.add_argument('--project-root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--no-rerender', action='store_true')
    parser.add_argument('--target')
    parser.add_argument('--dossier', type=Path)
    parser.add_argument('--in-place', action='store_true')
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    root = args.project_root.resolve()
    run = root / 'workspace/runs' / args.run_id
    cfg = _load_config(root)
    if args.command == 'build':
        print(build_target_dossiers(root, run, cfg))
    elif args.command == 'publish':
        print(publish_dossiers(root, run, cfg, force=args.force))
    elif args.command == 'check':
        print(verify_dossiers(root, run, cfg, rerender=not args.no_rerender))
    else:
        dossier = args.dossier or (output_root(root, cfg) / str(args.target))
        print(_reproduce(root, run, str(args.target), dossier, args.in_place))
    return 0


if __name__ == '__main__':
    sys.exit(main())

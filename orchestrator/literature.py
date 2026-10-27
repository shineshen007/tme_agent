"""Deterministic literature layer: query expansion, tool dispatch, and merging.

The agent layer may *propose* additional queries, but only this module executes retrieval,
with a fixed query budget, cache, and per-query provenance. Records are never turned into
evidence: stance stays 'unreviewed' and semantic assessment is a separate, review-pending field.
"""
from __future__ import annotations
import json
import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
import pandas as pd
from .io import read_json, write_json

LOG = logging.getLogger(__name__)

AXIS_QUERIES = {
    'tryptophan-kynurenine': 'hepatocellular carcinoma kynurenine immune',
    'lactate': 'hepatocellular carcinoma lactate immune',
    'cholesterol-lipid': 'hepatocellular carcinoma cholesterol immune',
    'arginine': 'hepatocellular carcinoma arginine immune',
    'nucleotide': 'hepatocellular carcinoma adenosine immune',
    'glutamine': 'hepatocellular carcinoma glutamine immune',
    'antioxidant': 'hepatocellular carcinoma glutathione immune',
    'catecholamine-3mt': 'hepatocellular carcinoma methoxytyramine immune',
}


def _top_terms(run, axes, cfg):
    """Candidate-driven query terms, each traceable to a frozen artifact and PubMed-searchable."""
    terms = {}
    briefs = cfg['llm']['briefs']
    symbols = {}
    audit_path = run / 'pathways/transcriptomics/triple109/gene_mapping_audit.csv'
    if audit_path.exists():
        audit = pd.read_csv(audit_path)
        if {'feature_id', 'symbol'} <= set(audit.columns):
            if 'selected_representative' in audit.columns:
                audit = audit[audit.selected_representative.fillna(False)]
            symbols = dict(zip(audit.feature_id.astype(str), audit.symbol.astype(str)))
    compounds = {}
    annotation_path = run / 'inputs/metabolite_annotations.csv'
    if annotation_path.exists():
        annotations = pd.read_csv(annotation_path)
        if {'feature_id', 'compound_name'} <= set(annotations.columns):
            compounds = dict(zip(annotations.feature_id.astype(str), annotations.compound_name.astype(str)))
    for omics, cohort in [('transcriptomics', 'triple109'), ('proteomics', 'triple109'), ('metabolomics', 'triple109')]:
        path = run / omics / cohort / 'de.csv'
        if not path.exists():
            continue
        d = pd.read_csv(path)
        if 'selected' not in d.columns or 'effect' not in d.columns:
            continue
        d = d[d.selected.fillna(False)].copy()
        top_n = {'transcriptomics': 6, 'proteomics': 4, 'metabolomics': 4}[omics]
        d['abs_effect'] = d.effect.abs()
        d = d.sort_values(['abs_effect', 'feature_id'], ascending=[False, True]).head(top_n)
        for feature in d.feature_id.astype(str):
            if omics == 'transcriptomics':
                term = symbols.get(feature, '')
            elif omics == 'metabolomics':
                term = compounds.get(feature, '')
            else:
                term = feature
            term = str(term).strip()
            if not term or term.upper().startswith('ENSG') or term.startswith('M') and term.endswith('_NEG'):
                continue
            terms.setdefault(term, {'axis': '', 'source': f'candidate:{omics}'})
    pathway_path = run / 'pathways/transcriptomics/triple109/gsea_full.csv'
    if pathway_path.exists():
        g = pd.read_csv(pathway_path)
        if {'pathway', 'NES'} <= set(g.columns):
            g = g.reindex(g.NES.abs().sort_values(ascending=False).index).head(briefs['pathway_top_n'])
            for name in g.pathway.astype(str):
                clean = name.replace('HALLMARK_', '').replace('_', ' ').lower()
                terms.setdefault(clean, {'axis': '', 'source': 'open_discovery_hallmark'})
    return terms


def build_queries(run, axes, cfg):
    queries = [{'query': q, 'axis': axis, 'source': 'prespecified_axis'} for axis, q in AXIS_QUERIES.items()]
    seen = {q['query'] for q in queries}
    for term, meta in _top_terms(run, axes, cfg).items():
        if len(queries) >= int(cfg['literature']['max_queries']):
            break
        query = f'hepatocellular carcinoma {term} immune'
        if query in seen:
            continue
        seen.add(query)
        queries.append({'query': query, 'axis': meta['axis'], 'source': meta['source']})
    return queries[:int(cfg['literature']['max_queries'])]


def _invoke(root, run, cfg, job, name):
    job_path = run / 'literature' / f'{name}.job.json'
    write_json(job_path, job)
    log_path = run / 'literature' / f'{name}.log'
    with log_path.open('w', encoding='utf-8') as log:
        subprocess.run([sys.executable, str(root / 'scripts/04_literature.py'), str(job_path)],
                       cwd=str(root), stdout=log, stderr=subprocess.STDOUT, check=True)
    return read_json(run / 'literature' / name / 'records.json')


def _merge(base, new, additional):
    """Merge frozen base records with newly retrieved records, keeping all provenance."""
    merged, index = [], {}
    for source, payload in [('frozen_base', base), ('deterministic_expansion', new), ('agent_proposed', additional)]:
        for record in (payload or {}).get('records', []):
            key = (record.get('axis', ''), str(record.get('pmid', '')))
            if not key[1]:
                continue
            if key in index:
                merged[index[key]].setdefault('also_retrieved_by', []).append(source)
                continue
            index[key] = len(merged)
            merged.append({**record, 'retrieval_source': source})
    return merged


def run_literature(root, run, cfg, axes):
    root, run = Path(root), Path(run)
    (run / 'literature').mkdir(parents=True, exist_ok=True)
    base_path = root / cfg['literature'].get('frozen_base', 'knowledge/literature/pubmed_candidates.json')
    base = read_json(base_path) if base_path.exists() else {'records': [], 'status': 'not_retrieved'}
    queries = build_queries(run, axes, cfg)
    job = {'project_root': str(root), 'output_dir': str(run / 'literature/base'), 'mode': 'base',
           'queries': queries, 'retmax_per_query': cfg['literature']['retmax_per_query'],
           'cache_dir': cfg['literature']['cache_dir'], 'eutils_base': cfg['literature']['eutils_base'],
           'request_pause_seconds': cfg['literature']['request_pause_seconds']}
    errors = []
    try:
        new = _invoke(root, run, cfg, job, 'base')
        errors.extend(new.get('errors', []))
    except Exception as exc:
        LOG.exception('Literature retrieval failed')
        new = {'records': [], 'errors': [{'query': 'base', 'error': str(exc)}]}
        errors.append({'query': 'base', 'error': str(exc)})
    records = _merge(base, new, None)
    summary = {'status': 'complete' if not errors else 'partial',
               'n_queries': len(queries), 'n_records': len(records),
               'n_new_records': len(new.get('records', [])),
               'n_frozen_base_records': len(base.get('records', [])),
               'accessed_at': datetime.now(timezone.utc).isoformat(),
               'errors': errors,
               'stance': 'unreviewed_requires_semantic_review'}
    write_json(run / 'literature/records.json', {**summary, 'records': records})
    return summary


def fetch_additional(root, run, cfg, additional):
    """Execute agent-proposed queries (bounded, logged, never model-issued network calls)."""
    root, run = Path(root), Path(run)
    limit = int(cfg['literature']['max_additional_queries'])
    queries = [{'query': q['query'], 'axis': q.get('axis', ''), 'source': 'agent_proposed'}
               for q in additional[:limit] if q.get('query')]
    if not queries:
        return {'status': 'skipped', 'n_records': 0}
    job = {'project_root': str(root), 'output_dir': str(run / 'literature/additional'), 'mode': 'additional',
           'queries': queries, 'retmax_per_query': cfg['literature']['retmax_per_query'],
           'cache_dir': cfg['literature']['cache_dir'], 'eutils_base': cfg['literature']['eutils_base'],
           'request_pause_seconds': cfg['literature']['request_pause_seconds']}
    try:
        payload = _invoke(root, run, cfg, job, 'additional')
    except Exception as exc:
        LOG.exception('Additional literature retrieval failed')
        payload = {'records': [], 'status': 'failed',
                   'errors': [{'query': 'additional', 'error': str(exc)}]}
    path = run / 'literature/records.json'
    current = read_json(path) if path.exists() else {'records': [], 'errors': []}
    errors = list(payload.get('errors', []))
    additional_status = payload.get('status')
    if additional_status == 'failed' and not errors:
        errors.append({'query': 'additional', 'error': 'Provider reported retrieval failure'})
    status = ('failed' if additional_status == 'failed' else 'partial' if errors or additional_status == 'partial'
              else 'complete' if payload.get('records') else 'no_results')
    merged = _merge({'records': current.get('records', [])}, None, payload)
    combined_errors = [*current.get('errors', []), *errors]
    summary = {**current, 'n_records': len(merged), 'n_additional_records': len(payload.get('records', [])),
               'additional_queries': queries, 'records': merged, 'errors': combined_errors,
               'additional_status': status,
               'status': ('partial' if merged else 'failed') if combined_errors else
                         'complete' if merged else 'no_results'}
    write_json(path, summary)
    return {'status': status, 'n_records': len(payload.get('records', [])),
            'queries': queries, 'errors': errors}

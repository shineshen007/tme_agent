"""Schema, artifact and scientific-contract checks; zero discoveries are valid."""
import json
import re
from collections import Counter
from pathlib import Path
import jsonschema
import numpy as np
import pandas as pd
from .io import sha256

def check_spoke_output(result: dict, schema_path: str) -> tuple[bool, list]:
    with open(schema_path) as f:
        schema = json.load(f)
    v = jsonschema.Draft7Validator(schema)
    errors = [e.message for e in v.iter_errors(result)]
    return (len(errors) == 0), errors


def validate_schema(result, path):
    ok, errors = check_spoke_output(result, str(path))
    if not ok:
        raise ValueError('; '.join(errors))


def evidence_v2_validator(schema_path):
    """Compile the v2 contract once for streaming registry validation."""
    with Path(schema_path).open(encoding='utf-8') as handle:
        schema = json.load(handle)
    jsonschema.Draft7Validator.check_schema(schema)
    return jsonschema.Draft7Validator(schema)


def validate_evidence_registry_v2(v1_csv, v2_jsonl, schema_path):
    """Validate every v2 row and exact v1 identity, direction and selection parity."""
    v1 = pd.read_csv(v1_csv, low_memory=False)
    if v1.evidence_id.duplicated().any():
        raise ValueError('Duplicate v1 evidence_id')
    validator = evidence_v2_validator(schema_path)
    remaining = v1.set_index('evidence_id')
    seen = set()
    count = 0
    with Path(v2_jsonl).open(encoding='utf-8') as handle:
        for line_number, line in enumerate(handle, 1):
            record = json.loads(line)
            errors = list(validator.iter_errors(record))
            if errors:
                raise ValueError(f'Invalid EvidenceRecord v2 line {line_number}: {errors[0].message}')
            eid = record['evidence_id']
            if eid in seen:
                raise ValueError(f'Duplicate v2 evidence_id: {eid}')
            seen.add(eid)
            count += 1
            if eid not in remaining.index:
                if record['source']['evidence_layer'] != 'scrna_context':
                    raise ValueError(f'Unexpected v2 evidence_id: {eid}')
                continue
            row = remaining.loc[eid]
            if record['source']['evidence_layer'] == 'scrna_context':
                raise ValueError(f'External context overlaps v1 evidence_id: {eid}')
            if record['observation']['direction'] != ('' if pd.isna(row.direction) else str(row.direction)):
                raise ValueError(f'v1/v2 direction mismatch: {eid}')
            if record['observation']['selected'] != bool(row.selected):
                raise ValueError(f'v1/v2 selected mismatch: {eid}')
            for key in ('effect', 'pvalue', 'padj', 'n_pairs'):
                old = row.get(key)
                new = record['observation'][key]
                if pd.isna(old):
                    if new is not None:
                        raise ValueError(f'v1/v2 {key} mismatch: {eid}')
                elif new != old:
                    raise ValueError(f'v1/v2 {key} mismatch: {eid}')
    missing = set(remaining.index) - seen
    if missing:
        raise ValueError(f'v2 missing {len(missing)} v1 evidence_id values')
    return {'n_v1': len(v1), 'n_v2': count, 'n_external': count - len(v1)}


def verify_artifacts(run, artifacts):
    run = Path(run).resolve()
    for a in artifacts:
        p = (run/a['path']).resolve()
        if not p.is_relative_to(run) or not p.is_file() or sha256(p) != a['sha256'] or p.stat().st_size != a['bytes']:
            raise ValueError(f'Artifact verification failed: {a["path"]}')


def validate_de(path):
    d = pd.read_csv(path)
    if d.feature_id.duplicated().any():
        raise ValueError('Duplicate tested features')
    for column in ['pvalue','padj']:
        if not d[column].dropna().between(0,1).all():
            raise ValueError(f'Invalid {column}')
    if not d.n_pairs.ge(3).all() or not d.contrast.eq('tumor_vs_adjacent').all():
        raise ValueError('Invalid cohort or contrast')
    estimated=d.effect.notna()
    if not np.isfinite(d.loc[estimated,'effect']).all():
        raise ValueError('Nonfinite effect')
    ci=d.ci_low.notna() & d.ci_high.notna()
    if not ((d.loc[ci,'ci_low'] <= d.loc[ci,'effect']) & (d.loc[ci,'effect'] <= d.loc[ci,'ci_high'])).all():
        raise ValueError('Effect outside confidence interval')
    return {'n_features':len(d),'n_tested':int(d.padj.notna().sum()),'n_selected':int(d.selected.sum())}


def validate_hypotheses(payload, registry, schema):
    validate_schema(payload,schema)
    if registry.evidence_id.duplicated().any():
        raise ValueError('Evidence ID collision')
    by=registry.set_index('evidence_id')
    for h in payload['hypotheses']:
        ids=h['evidence_ids']+h['counterevidence_ids']
        if not set(ids)<=set(by.index):
            raise ValueError('Hypothesis cites unknown evidence')
        actual=set(by.loc[h['evidence_ids'],'omics'])
        if actual!=set(h['independent_omics']) or len(actual)<2:
            raise ValueError('At least two distinct measured omics required')
        if not by.loc[h['evidence_ids'],'cohort_id'].eq('triple109').all():
            raise ValueError('Hypothesis mixes cohorts')
        if not by.loc[h['evidence_ids'],'selected'].all():
            raise ValueError('Supporting evidence not selected by frozen thresholds')
        if h.get('external_scrna_context') is not None:
            ctx=h['external_scrna_context']
            if ctx.get('boundary')!='external_pan_cancer_atlas_context_not_independent_validation':
                raise ValueError('scRNA context must carry the external-atlas boundary label')
            if set(ctx.get('evidence_ids',[])) & set(by.index):
                raise ValueError('scRNA context must not cite bulk evidence IDs')


def validate_prognosis_outputs(prognosis_dir, stats):
    """Structural and numeric contract for the pre-registered candidate-axis prognosis branch.

    Every tested feature must be present, including not-estimable models: the branch is
    cohort tracking with frozen thresholds, so completeness is the contract, not significance.
    """
    prognosis_dir = Path(prognosis_dir)
    if stats.get('status') != 'complete':
        raise ValueError('Prognosis tool did not report complete status')
    cox = pd.read_csv(prognosis_dir / 'cox_models.csv')
    required = ['cohort', 'outcome', 'model_type', 'feature_id', 'omics', 'entity', 'axis',
                'effect_scale', 'n', 'n_events', 'hr', 'ci_low', 'ci_high', 'pvalue', 'status']
    for column in required:
        if column not in cox.columns:
            raise ValueError(f'cox_models.csv missing column: {column}')
    if not cox.status.isin(['tested', 'not_estimable']).all():
        raise ValueError('Unexpected Cox model status value')
    if not cox.outcome.isin(['os', 'rfs']).all() or not cox.model_type.isin(['univariable', 'adjusted']).all():
        raise ValueError('Unexpected Cox model outcome or model_type')
    if not cox.effect_scale.eq('hazard_ratio_per_sd_tumor_value').all():
        raise ValueError('Cox models must report hazard ratios per SD of tumor values')
    if not cox.n.ge(0).all() or not cox.n_events.ge(0).all():
        raise ValueError('Invalid model sample or event counts')
    tested = cox.status.eq('tested')
    if tested.any():
        for column in ['pvalue', 'ci_low', 'ci_high', 'hr']:
            if not cox.loc[tested, column].notna().all():
                raise ValueError(f'Tested Cox model is missing {column}')
        if not cox.loc[tested, 'pvalue'].between(0, 1).all():
            raise ValueError('Invalid Cox p-value')
        if not cox.loc[tested, 'hr'].gt(0).all():
            raise ValueError('Hazard ratio must be positive')
        if not ((cox.loc[tested, 'ci_low'] <= cox.loc[tested, 'hr']) & (cox.loc[tested, 'hr'] <= cox.loc[tested, 'ci_high'])).all():
            raise ValueError('Hazard ratio outside confidence interval')
    clinical = pd.read_csv(prognosis_dir / 'patient_clinical.csv')
    for column in ['patient_id', 'os_months', 'os_event', 'rfs_months', 'rfs_event', 'tnm_ordinal']:
        if column not in clinical.columns:
            raise ValueError(f'patient_clinical.csv missing column: {column}')
    if clinical.patient_id.duplicated().any():
        raise ValueError('Duplicate patient in patient_clinical.csv')
    features = pd.read_csv(prognosis_dir / 'patient_features.csv')
    for column in ['patient_id', 'value_type', 'omics', 'entity', 'feature_id', 'panel', 'value']:
        if column not in features.columns:
            raise ValueError(f'patient_features.csv missing column: {column}')
    if not features.value_type.isin(['axis_feature', 'tcell_proportion', 'tcell_signature']).all():
        raise ValueError('Unexpected value_type in patient_features.csv')
    if features.duplicated(['patient_id', 'value_type', 'feature_id', 'panel']).any():
        raise ValueError('Duplicate patient feature rows')
    axis_rows = features.loc[features.value_type == 'axis_feature']
    if len(axis_rows) and not np.isfinite(axis_rows.value.astype(float).to_numpy()).all():
        raise ValueError('Nonfinite axis feature values')
    return {'n_cox_rows': len(cox), 'n_tested': int(tested.sum()),
            'n_patients': int(clinical.patient_id.nunique()), 'n_feature_rows': len(features)}


def validate_scrna_outputs(scrna_dir, stats):
    """Structural and numeric contract for the external atlas branch outputs."""
    scrna_dir=Path(scrna_dir)
    if stats.get('status')!='complete':
        raise ValueError('scRNA tool did not report complete status')
    analyses={a['name']:a for a in stats.get('analyses',[])}
    for name in ['inventory','axis_localization','deconvolution']:
        if name not in analyses:
            raise ValueError(f'Missing scRNA analysis section: {name}')
    loc=pd.read_csv(scrna_dir/'axis_localization.csv')
    for column in ['gene','celltype','status','claim_type','cohort_id','donor_pvalue','donor_padj']:
        if column not in loc.columns:
            raise ValueError(f'axis_localization.csv missing column: {column}')
    if not loc.claim_type.eq('external_context').all() or not loc.cohort_id.eq('external_atlas').all():
        raise ValueError('scRNA localization rows must be labeled external_context/external_atlas')
    if loc.duplicated(['gene','celltype']).any():
        raise ValueError('Duplicate gene x celltype localization tests')
    for column in ['donor_pvalue','donor_padj','cell_level_pvalue','cell_level_padj']:
        if column in loc and not loc[column].dropna().between(0,1).all():
            raise ValueError(f'Invalid {column} in axis_localization')
    paired=pd.read_csv(scrna_dir/'deconvolution/deconvolution_paired.csv')
    if not {'panel','celltype','effect','pvalue','padj','n_pairs','claim_type'} <= set(paired.columns):
        raise ValueError('deconvolution_paired.csv missing required columns')
    if not paired.claim_type.eq('estimate_not_measured_fraction').all():
        raise ValueError('Deconvolution rows must be labeled as estimates')
    if not paired.pvalue.dropna().between(0,1).all() or not paired.padj.dropna().between(0,1).all():
        raise ValueError('Invalid deconvolution p-values')
    for panel_file in (scrna_dir/'deconvolution').glob('cell_proportions_*.csv'):
        props=pd.read_csv(panel_file)
        celltypes=[c for c in props.columns if c not in {'biospecimen_id','patient_id','tissue','panel'}]
        values=props[celltypes].to_numpy(dtype=float)
        finite_rows=np.isfinite(values).all(axis=1)
        if finite_rows.any():
            v=values[finite_rows]
            if (v<-1e-9).any():
                raise ValueError(f'Negative proportions in {panel_file.name}')
            if not np.allclose(v.sum(axis=1),1,atol=1e-6):
                raise ValueError(f'Proportions do not sum to 1 in {panel_file.name}')
    return {'n_localization_rows':len(loc),'n_paired_rows':len(paired)}


# --------------------------------------------------------------------------------------
# Agent-layer guardrails. The model may only cite evidence and describe direction; every
# number in the final report is injected from the registry by a deterministic renderer.
# --------------------------------------------------------------------------------------

NUMBER_RE = re.compile(
    r'(\bp\s*[<>=]\s*\d)|(\bpadj\b)|(\bq[- ]?value\b)|(\bFDR\b)|(\blog2\s*fc\b)|(\blog2FC\b)'
    r'|(\bfold[- ]?change\b)|(\d+\.\d+)|(\d+[eE][-+]?\d+)|(\d+\s*%)', re.IGNORECASE)
# Abbreviations such as OR/HR/CI/NES are common English words; require uppercase + an explicit
# value so ordinary prose cannot trip the statistics check.
STAT_ABBREV_RE = re.compile(r'\b(NES|HR|OR|CI)\s*[:=]\s*[-+]?[\d.]+')
# Evidence ids are sha256 hex prefixes and can contain scientific-notation-like substrings
# (e.g. "…1e253…"). They are validated by existence, so they must not trip the number check.
EVIDENCE_ID_TOKEN = re.compile(r'ev_[0-9a-f]{20}')
FORBIDDEN_CAUSAL = ['导致', '驱动', '介导', '调控', '证明', '证实', 'causes', 'drives', 'mediates', 'regulates', 'proves']
QUALIFIERS = ['未验证', '候选', '观察性', '不证明', '不构成', '待验证', '可能', '提示', '推测', '假设', '潜在',
              '疑似', '有待', '尚未', 'not validated', 'candidate', 'observational', 'correlat', 'may ', 'suggest']
# Fields that are hypothetical by construction: competing explanations, risks and open questions.
# Causal wording there is not an asserted finding, so the strict rule applies to claim fields only.
HYPOTHETICAL_KEYS = {'alternative_explanations', 'unknowns', 'caveats', 'notes', 'technical_caveats',
                     'open_questions'}


def _walk_strings(node, path=()):
    if isinstance(node, str):
        yield path, node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield from _walk_strings(value, path + (key,))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk_strings(value, path + (index,))


def validate_no_numbers(payload):
    """Statistics numbers must never be authored by the model (quoted literature text excepted)."""
    violations = []
    for path, text in _walk_strings(payload):
        if path and path[-1] == 'quote':
            continue
        cleaned = EVIDENCE_ID_TOKEN.sub(' ', text)
        hit = NUMBER_RE.search(cleaned) or STAT_ABBREV_RE.search(cleaned)
        if hit:
            violations.append({'path': '.'.join(str(p) for p in path), 'match': hit.group(0)})
    if violations:
        raise ValueError(f'Agent output contains statistics-like numbers: {violations[:5]}')


def validate_language(payload):
    """Causal/mechanistic verbs are only allowed when hedged or in explicitly hypothetical fields."""
    problems = []
    for path, text in _walk_strings(payload):
        if any(part in HYPOTHETICAL_KEYS for part in path):
            continue
        low = text.lower()
        for verb in FORBIDDEN_CAUSAL:
            if verb.lower() in low and not any(q in low for q in QUALIFIERS):
                problems.append({'path': '.'.join(str(p) for p in path), 'verb': verb})
    if problems:
        raise ValueError(f'Unqualified causal language: {problems[:5]}')


def redact_check(text, private_pattern):
    if private_pattern is None:
        return
    hit = private_pattern.search(text)
    if hit:
        raise ValueError(f'Outbound text contains a cohort identifier: {hit.group(1)}')


def validate_agent_analyst(payload, registry, schema_path):
    validate_schema(payload, schema_path)
    known = set(registry.evidence_id)
    for finding in payload['key_findings']:
        if not finding['evidence_ids']:
            raise ValueError(f'Analyst finding {finding["finding_id"]} cites no evidence')
        unknown = [e for e in finding['evidence_ids'] if e not in known]
        if unknown:
            raise ValueError(f'Analyst cites unknown evidence ids: {unknown[:5]}')
    validate_no_numbers(payload)
    validate_language(payload)


def _check_hypothesis(h, registry, by, known, axis_evidence, literature_pmids):
    cited = list(h['supporting_evidence_ids']) + list(h['counterevidence_ids'])
    unknown = [e for e in cited if e not in known]
    if unknown:
        raise ValueError(f'cites unknown evidence: {unknown[:5]}')
    support = h['supporting_evidence_ids']
    # Citation completeness: an evidence id mentioned in the prose must also be listed.
    mentioned = set(EVIDENCE_ID_TOKEN.findall(f"{h.get('statement', '')} {h.get('rationale', '')}"))
    unlisted = mentioned - set(cited)
    if unlisted:
        raise ValueError(f'mentions evidence ids not listed in support/counter: {sorted(unlisted)[:5]}')
    layer_of = (dict(zip(by.index.astype(str), by['evidence_layer'].fillna('de').astype(str)))
                if 'evidence_layer' in by.columns else {})
    claim_of = (dict(zip(by.index.astype(str), by['claim_type'].fillna('association').astype(str)))
                if 'claim_type' in by.columns else {})
    measured = [e for e in support
                if layer_of.get(str(e), 'de') == 'de'
                and claim_of.get(str(e), 'association') != 'estimate_association'
                and by.loc[e, 'omics'] in {'transcriptomics', 'proteomics', 'metabolomics'}]
    omics = set(by.loc[measured, 'omics'])
    if len(omics) < 2:
        raise ValueError('requires at least two distinct measured omics (estimate-based correlations excluded)')
    if not by.loc[support, 'cohort_id'].eq('triple109').all():
        raise ValueError('mixes cohorts')
    if not by.loc[support, 'selected'].astype(bool).all():
        raise ValueError('cites evidence not selected by frozen thresholds')
    if h['validation_status'] != 'not_independently_validated':
        raise ValueError('validation_status is immutable and must stay not_independently_validated')
    # Pre-registered candidate-axis layers must stay anchored to the bulk tissue contrast:
    # a prognosis or T-cell correlation claim is only admissible with a same-axis selected DE row.
    if set(layer_of.get(str(e), '') for e in cited) & {'prognosis', 'tcell_correlation'}:
        de_support = [e for e in support if layer_of.get(str(e), 'de') == 'de']
        if not de_support:
            raise ValueError('prognosis or T-cell correlation claims require a selected bulk DE anchor evidence')
        if axis_evidence is not None and len(axis_evidence):
            axis_ids = set(axis_evidence.loc[axis_evidence.axis == h['axis'], 'evidence_id'].astype(str))
            if not (set(map(str, de_support)) & axis_ids):
                raise ValueError('prognosis or T-cell correlation claims require a same-axis bulk DE anchor')
    if axis_evidence is not None and len(axis_evidence):
        # Anti-cherry-picking: every selected same-axis finding whose direction contradicts the
        # hypothesis direction must be cited (as support or counter). Concordant findings may be
        # summarised; they are not required to be listed exhaustively. Primary (univariable,
        # triple109) prognosis findings join the same consistency table; sensitivity rows do not.
        axis_rows = axis_evidence[axis_evidence.axis == h['axis']]
        direction_of = dict(zip(axis_rows.evidence_id.astype(str), axis_rows.direction.astype(str)))
        selected_ids = set(axis_rows.loc[axis_rows.selected.astype(bool), 'evidence_id'].astype(str))
        if {'axis', 'evidence_layer', 'model_type'} <= set(by.columns):
            primary = by[(by.axis == h['axis']) & (by.evidence_layer == 'prognosis') &
                         (by.cohort_id == 'triple109') & (by.model_type == 'univariable') &
                         (by.selected.fillna(False).astype(bool)) & (by.direction.isin(['up', 'down']))]
            direction_of.update(dict(zip(primary.index.astype(str), primary.direction.astype(str))))
            selected_ids |= set(primary.index.astype(str))
        support_directions = [direction_of[e] for e in support if direction_of.get(str(e)) in {'up', 'down'}]
        if support_directions:
            majority = Counter(support_directions).most_common(1)[0][0]
            discordant = {e for e in selected_ids
                          if direction_of.get(e) in {'up', 'down'} and direction_of.get(e) != majority}
            omitted = discordant - set(map(str, support)) - set(map(str, h['counterevidence_ids']))
            if omitted:
                raise ValueError(f'omits contradicting same-axis evidence: {sorted(omitted)[:5]}')
    if literature_pmids is not None:
        bad = [p for p in h['literature_pmids'] if p not in literature_pmids]
        if bad:
            raise ValueError(f'cites unknown PMIDs: {bad[:5]}')


def _check_targets(payload, known):
    for group in payload['target_groups']:
        for target in group['targets']:
            unknown = [e for e in target['evidence_ids'] if e not in known]
            if unknown:
                raise ValueError(f'Target {target["identifier"]} cites unknown evidence: {unknown[:5]}')
            if {'priority', 'rank', 'score', 'order'} & set(target):
                raise ValueError('Targets must not carry ranking or priority fields')


def _check_stances(payload, literature_pmids, literature_records=None):
    """Optionally check quotes against frozen source text, not only PMID membership.

    Legacy callers can omit records. Text verification does not establish that a
    quotation semantically supports the hypothesis or matches its study context.
    """
    if literature_records is not None:
        from .research_literature import _normalise, record_passages, verify_source_record
        records = (literature_records.get('records', []) if isinstance(literature_records, dict)
                   else literature_records)
        by_pmid = {}
        for record in records:
            by_pmid.setdefault(str(record.get('pmid', '')), []).append(record)
    for stance in payload['literature_stances']:
        if stance['pmid'] not in (literature_pmids or set()):
            raise ValueError(f'Stance cites unknown PMID: {stance["pmid"]}')
        if stance['stance'] in {'supports', 'contradicts'} and not stance['quote'].strip():
            raise ValueError(f'Stance for PMID {stance["pmid"]} asserts a position without a quote')
        if literature_records is not None and stance['quote'].strip():
            sources = by_pmid.get(str(stance['pmid']), [])
            passages = []
            for source in sources:
                if 'passages' in source or 'source_text_sha256' in source:
                    verify_source_record(source)
                passages.extend(record_passages(source))
            if not any(_normalise(stance['quote']) in _normalise(p['text']) for p in passages):
                raise ValueError(f'Stance quote does not match frozen source text for PMID {stance["pmid"]}')


def validate_agent_synthesis(payload, registry, schema_path, axis_evidence=None, literature_pmids=None,
                             literature_records=None):
    """Strict form: any rule violation rejects the whole synthesis."""
    validate_schema(payload, schema_path)
    known = set(registry.evidence_id)
    by = registry.set_index('evidence_id')
    for h in payload['hypotheses']:
        try:
            _check_hypothesis(h, registry, by, known, axis_evidence, literature_pmids)
        except ValueError as exc:
            raise ValueError(f'Hypothesis {h.get("id", "?")} {exc}') from exc
    _check_targets(payload, known)
    _check_stances(payload, literature_pmids, literature_records)
    validate_no_numbers(payload)
    validate_language(payload)


def filter_agent_hypotheses(payload, registry, schema_path, axis_evidence=None, literature_pmids=None,
                            literature_records=None):
    """Envelope stays strict; rule-violating hypotheses are dropped and reported, never silently.

    A single non-compliant hypothesis must not discard the compliant ones, but it also must never
    reach the report: offenders are returned with reasons for an explicit rejection ledger.
    """
    validate_schema(payload, schema_path)
    known = set(registry.evidence_id)
    by = registry.set_index('evidence_id')
    _check_targets(payload, known)
    _check_stances(payload, literature_pmids, literature_records)
    validate_no_numbers(payload)
    validate_language(payload)
    kept, rejected = [], []
    for h in payload['hypotheses']:
        try:
            _check_hypothesis(h, registry, by, known, axis_evidence, literature_pmids)
            kept.append(h)
        except ValueError as exc:
            rejected.append({'hypothesis_id': str(h.get('id', '?')), 'reason': str(exc)})
    payload['hypotheses'] = kept
    return rejected


def validate_agent_review(payload, schema_path):
    validate_schema(payload, schema_path)
    validate_no_numbers(payload)
    issues = payload['issues']
    ids = [issue['issue_id'] for issue in issues]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate critic issue_id')
    decision = payload['decision']
    routes = {issue['resolvable_by'] for issue in issues}
    if decision == 'accept' and (issues or payload['unresolved_issues']):
        raise ValueError('Critic accept requires no issues or unresolved issues')
    if decision == 'revise_reasoning' and (not issues or routes != {'reasoning_revision'}):
        raise ValueError('Critic revision requires only reasoning_revision issues')
    if decision == 'request_analysis' and ('reanalysis' not in routes or 'human_review' in routes):
        raise ValueError('Critic analysis request requires a reanalysis issue and no human_review issue')
    if decision != 'block' and 'human_review' in routes:
        raise ValueError('Human-review issue requires block')
    if decision != 'block' and any(i['category'] == 'evidence_rule' and
                                   i['severity'] == 'blocking' for i in issues):
        raise ValueError('Blocking evidence rule requires block')
    if decision == 'block' and not (issues or payload['unresolved_issues']):
        raise ValueError('Critic block requires a stated unresolved reason')
    return {'n_issues': len(issues),
            'n_blocking': sum(1 for i in issues if i['severity'] == 'blocking')}


MEASURED_BULK_OMICS = {'transcriptomics', 'proteomics', 'metabolomics'}


def _axis_de_rows(axis_evidence, axis):
    """Frozen same-axis bulk DE rows; no statistics are recalculated."""
    if axis_evidence is None or axis_evidence.empty:
        return pd.DataFrame()
    rows = axis_evidence[axis_evidence.axis.astype(str).eq(axis)].copy()
    if 'evidence_layer' in rows:
        rows = rows[rows.evidence_layer.fillna('de').eq('de')]
    return rows[rows.omics.isin(MEASURED_BULK_OMICS) & rows.cohort_id.eq('triple109')]


def validate_evidence_clusters(payload, registry, schema_path, axis_evidence=None, axes=None):
    """Accept only a complete qualitative projection of frozen axis evidence."""
    validate_schema(payload, schema_path)
    validate_no_numbers(payload)
    validate_language(payload)
    known = set(registry.evidence_id.astype(str))
    by = registry.set_index('evidence_id')
    permitted_axes = {str(a['axis']) for a in axes} if axes is not None else None
    cluster_ids, cluster_axes = set(), set()
    for cluster in payload['evidence_clusters']:
        cid, axis = cluster['cluster_id'], cluster['axis']
        if cid in cluster_ids or axis in cluster_axes:
            raise ValueError(f'Duplicate cluster ID or axis: {cid}, {axis}')
        cluster_ids.add(cid)
        cluster_axes.add(axis)
        if permitted_axes is not None and axis not in permitted_axes:
            raise ValueError(f'Unknown cluster axis: {axis}')
        support, counter = set(cluster['supporting_evidence_ids']), set(cluster['counterevidence_ids'])
        cited = support | counter
        if support & counter:
            raise ValueError(f'Cluster {cid} lists evidence as both support and counter')
        if cited - known:
            raise ValueError(f'Cluster {cid} cites unknown evidence: {sorted(cited - known)[:5]}')
        axis_rows = _axis_de_rows(axis_evidence, axis)
        selected_de = set(axis_rows.loc[axis_rows.selected.fillna(False).astype(bool), 'evidence_id'].astype(str)) \
            if len(axis_rows) else set()
        if selected_de - cited:
            raise ValueError(f'Cluster {cid} omits selected same-axis evidence: {sorted(selected_de - cited)[:5]}')
        axis_ids = set(axis_rows.evidence_id.astype(str)) if len(axis_rows) else set()
        if 'axis' in registry.columns:
            axis_ids |= set(registry.loc[registry.axis.astype(str).eq(axis), 'evidence_id'].astype(str))
        if axis_evidence is not None and len(axis_evidence):
            axis_ids |= set(axis_evidence.loc[axis_evidence.axis.astype(str).eq(axis), 'evidence_id'].astype(str))
        if cited - axis_ids:
            raise ValueError(f'Cluster {cid} cites evidence outside axis {axis}: {sorted(cited - axis_ids)[:5]}')
        if support and not by.loc[list(support), 'selected'].fillna(False).astype(bool).all():
            raise ValueError(f'Cluster {cid} supports with unselected evidence')
        measured = {str(by.loc[e, 'omics']) for e in cited
                    if str(by.loc[e].get('evidence_layer', 'de')) in {'de', 'nan'}
                    and str(by.loc[e, 'cohort_id']) == 'triple109'
                    and bool(by.loc[e, 'selected'])
                    and str(by.loc[e, 'omics']) in MEASURED_BULK_OMICS}
        if set(cluster['measured_omics']) != measured:
            raise ValueError(f'Cluster {cid} measured_omics disagrees with frozen DE rows')
        for omics in MEASURED_BULK_OMICS:
            subset = axis_rows[axis_rows.omics.eq(omics)] if len(axis_rows) else axis_rows
            expected = ('selected' if len(subset) and subset.selected.fillna(False).astype(bool).any()
                        else 'observed_unselected' if len(subset) else 'unavailable')
            if cluster['modality_coverage'][omics] != expected:
                raise ValueError(f'Cluster {cid} modality_coverage disagrees for {omics}')
        allowed_entities = set(by.loc[list(cited), 'feature_id'].dropna().astype(str)) if cited else set()
        if len(axis_rows) and 'entity' in axis_rows:
            allowed_entities |= set(axis_rows.entity.dropna().astype(str))
        if 'entity' in by.columns and cited:
            allowed_entities |= set(by.loc[list(cited), 'entity'].dropna().astype(str))
        if set(cluster['entities']) - allowed_entities:
            raise ValueError(f'Cluster {cid} names an unregistered entity')
        support_directions = [str(by.loc[e, 'direction']) for e in cluster['supporting_evidence_ids']
                              if e in selected_de and str(by.loc[e, 'direction']) in {'up', 'down'}]
        if support_directions:
            majority = Counter(support_directions).most_common(1)[0][0]
            opposing = set(axis_rows.loc[axis_rows.selected.fillna(False).astype(bool) &
                                          axis_rows.direction.isin(['up', 'down']) &
                                          axis_rows.direction.ne(majority), 'evidence_id'].astype(str)) \
                if len(axis_rows) else set()
            if {'axis', 'evidence_layer', 'model_type'} <= set(registry.columns):
                primary = registry[(registry.axis == axis) & (registry.evidence_layer == 'prognosis') &
                                   (registry.cohort_id == 'triple109') &
                                   (registry.model_type == 'univariable') &
                                   (registry.selected.fillna(False).astype(bool)) &
                                   (registry.direction.isin(['up', 'down'])) &
                                   (registry.direction.ne(majority))]
                opposing |= set(primary.evidence_id.astype(str))
            if opposing - counter:
                raise ValueError(f'Cluster {cid} omits contradicting same-axis evidence: {sorted(opposing - counter)[:5]}')
        directions = set(axis_rows.loc[axis_rows.selected.fillna(False).astype(bool), 'direction'].astype(str)) \
            if len(axis_rows) else set()
        if cluster['concordance'] == 'concordant' and (len(measured) < 2 or {'up', 'down'} <= directions):
            raise ValueError(f'Cluster {cid} overstates concordance')
    if axis_evidence is not None and len(axis_evidence):
        required_axes = set(axis_evidence.loc[axis_evidence.selected.fillna(False).astype(bool), 'axis'].dropna().astype(str))
        if required_axes - cluster_axes:
            raise ValueError(f'Missing clusters for selected axes: {sorted(required_axes - cluster_axes)[:5]}')


def adapt_reasoner_output(payload, clusters, registry, reasoner_schema, synthesis_schema,
                           axis_evidence=None, literature_pmids=None, literature_records=None):
    """Validate private cluster references, then return a legacy synthesis and rejection ledger."""
    validate_schema(payload, reasoner_schema)
    validate_no_numbers(payload)
    validate_language(payload)
    by_cluster = {c['cluster_id']: c for c in clusters['evidence_clusters']}
    available = {e for c in by_cluster.values()
                 for e in c['supporting_evidence_ids'] + c['counterevidence_ids']}
    for group in payload['target_groups']:
        for target in group['targets']:
            mentioned = {e for _, value in _walk_strings(target) for e in EVIDENCE_ID_TOKEN.findall(value)}
            if set(target['evidence_ids']) - available or mentioned - available:
                raise ValueError(f'Target cites evidence absent from Integrator: {target["identifier"]}')
    legacy = {key: payload[key] for key in ('target_groups', 'literature_stances')}
    legacy['hypotheses'] = []
    rejected = []
    for h in payload['hypotheses']:
        try:
            cluster = by_cluster.get(h['cluster_id'])
            if cluster is None or h['axis'] != cluster['axis']:
                raise ValueError('references an unknown or mismatched EvidenceCluster')
            mentioned = {e for _, value in _walk_strings(h) for e in EVIDENCE_ID_TOKEN.findall(value)}
            if mentioned - available:
                raise ValueError('mentions evidence absent from Integrator')
            if set(h['supporting_evidence_ids']) - set(cluster['supporting_evidence_ids']):
                raise ValueError('support cites evidence absent from linked EvidenceCluster')
            if set(h['counterevidence_ids']) - set(cluster['counterevidence_ids']):
                raise ValueError('counter cites evidence absent from linked EvidenceCluster')
            if set(h['referenced_missing_links']) - set(cluster['missing_links']):
                raise ValueError('invents a missing link absent from linked EvidenceCluster')
            if set(h['unknowns']) - set(cluster['missing_links'] + cluster['open_questions']):
                raise ValueError('unknowns invent a missing link or question absent from linked EvidenceCluster')
            legacy['hypotheses'].append({k: v for k, v in h.items()
                                          if k not in {'cluster_id', 'referenced_missing_links'}})
        except ValueError as exc:
            rejected.append({'hypothesis_id': str(h.get('id', '?')), 'reason': str(exc)})
    rejected.extend(filter_agent_hypotheses(legacy, registry, synthesis_schema,
                                            axis_evidence=axis_evidence, literature_pmids=literature_pmids,
                                            literature_records=literature_records))
    return legacy, rejected

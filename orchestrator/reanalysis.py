"""Read-only dispatch for structured questions about frozen analysis artifacts.

This module never invokes a statistical tool or writes into a run. A matching
result cites an existing EvidenceRecord; it is not a new independent result.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

import jsonschema

from .checkers import evidence_v2_validator, validate_schema
from .io import read_json, sha256


ANALYSIS_LAYERS = {
    'de_evidence_lookup': 'de',
    'scrna_context_lookup': 'scrna_context',
    'prognosis_association_lookup': 'prognosis',
    'tcell_correlation_lookup': 'tcell_correlation',
}
MAX_REQUESTS_PER_ITERATION = 10


class ReanalysisPlanner:
    """Validate a request, then look up only manifest-verified frozen rows.

    ``history`` passed to ``execute`` is the sequence of previously accepted
    request dictionaries. A future graph must persist and pass that history;
    this standalone stage does not create a loop or write a request ledger.
    """

    def __init__(self, root, run, cfg, *, frozen_manifest=None, hypothesis_ids=None):
        self.root = Path(root).resolve()
        self.run = Path(run).resolve()
        self.cfg = cfg
        reasoning = cfg.get('reasoning', {})
        current = reasoning.get('max_iterations')
        alias = reasoning.get('max_reasoning_iterations')
        if current is not None and alias is not None and current != alias:
            raise ValueError('reasoning iteration limits disagree')
        limit = current if current is not None else (alias if alias is not None else 2)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError('max_iterations must be a positive integer')
        self.max_iterations = limit
        request_limit = reasoning.get('max_reanalysis_requests_per_iteration', MAX_REQUESTS_PER_ITERATION)
        if isinstance(request_limit, bool) or not isinstance(request_limit, int) or request_limit < 1:
            raise ValueError('max_reanalysis_requests_per_iteration must be a positive integer')
        self.max_requests_per_iteration = request_limit
        self.frozen_manifest = Path(frozen_manifest).resolve() if frozen_manifest is not None else None
        if self.frozen_manifest is not None and not self.frozen_manifest.is_relative_to(self.run):
            raise ValueError('Reasoning snapshot must belong to this run')
        self.hypothesis_ids = set(hypothesis_ids) if hypothesis_ids is not None else None
        self.request_schema = self.root / 'schemas/reanalysis_request.schema.json'
        self.result_schema = self.root / 'schemas/reanalysis_result.schema.json'
        self.evidence_schema = self.root / 'schemas/evidence_v2.schema.json'
        with self.request_schema.open(encoding='utf-8') as handle:
            schema = json.load(handle)
        jsonschema.Draft7Validator.check_schema(schema)
        self.request_validator = jsonschema.Draft7Validator(schema)

    def _result(self, request, status, reason_code, ids=(), rows=()):
        result = {'schema_version': '1.0',
                  'request_id': str(request.get('request_id', 'invalid')),
                  'iteration': request.get('iteration') if isinstance(request.get('iteration'), int)
                  and not isinstance(request.get('iteration'), bool) else None,
                  'analysis_type': request.get('analysis_type') if isinstance(request.get('analysis_type'), str) else None,
                  'status': status, 'reason_code': reason_code,
                  'referenced_evidence_ids': sorted(set(ids)),
                  'source_rows': sorted({(r['artifact_path'], r['row_key'], r['sha256']): r for r in rows}.values(),
                                        key=lambda r: (r['artifact_path'], r['row_key'])),
                  'new_evidence': []}
        validate_schema(result, self.result_schema)
        return result

    def _manifest(self):
        manifest = read_json(self.frozen_manifest or self.run / 'artifact_manifest.json')
        entries = manifest.get('artifacts', [])
        if len({a['path'] for a in entries}) != len(entries):
            raise ValueError('Duplicate artifact path in frozen manifest')
        return {a['path']: a for a in entries}

    def _frozen_path(self, relative, manifest):
        # All callers pass code-owned paths or v2 provenance, never a request path.
        path = (self.run / relative).resolve()
        if not path.is_relative_to(self.run):
            raise ValueError(f'Artifact path escapes frozen run: {relative}')
        entry = manifest.get(relative)
        if entry is None or not path.is_file():
            raise ValueError(f'Artifact absent from frozen manifest: {relative}')
        if path.stat().st_size != entry['bytes'] or sha256(path) != entry['sha256']:
            raise ValueError(f'Frozen artifact hash mismatch: {relative}')
        return path, entry['sha256']

    @staticmethod
    def _query_key(request):
        return (request['analysis_type'], request['target_omics'],
                tuple(sorted(request['features'])), tuple(sorted(request['pathways'])))

    def _check_history(self, request, history):
        if request['iteration'] > self.max_iterations:
            return 'iteration_limit_exceeded'
        if not isinstance(history, (list, tuple)):
            raise ValueError('Request history must be a sequence')
        for prior in history:
            if list(self.request_validator.iter_errors(prior)):
                raise ValueError('Request history contains an invalid request')
        ids = [prior['request_id'] for prior in history]
        if len(set(ids)) != len(ids):
            raise ValueError('Request history contains duplicate request_id values')
        if request['request_id'] in ids:
            return 'duplicate_request_id'
        if self._query_key(request) in {self._query_key(prior) for prior in history}:
            return 'duplicate_query'
        highest = max((prior['iteration'] for prior in history), default=0)
        if request['iteration'] not in {highest, highest + 1} or (highest == 0 and request['iteration'] != 1):
            return 'invalid_iteration_sequence'
        if sum(prior['iteration'] == request['iteration'] for prior in history) >= self.max_requests_per_iteration:
            return 'request_limit_exceeded'
        return None

    @staticmethod
    def _source_key(row, layer):
        if layer == 'scrna_context':
            return f"{row.get('gene', '')}|{row.get('celltype', '')}"
        return row.get('feature_id', '') if layer == 'de' else row.get('evidence_id', '')

    def _source_status(self, path, row_key, layer, cache):
        if path not in cache:
            with path.open(newline='', encoding='utf-8') as handle:
                cache[path] = list(csv.DictReader(handle))
        hits = [row for row in cache[path] if self._source_key(row, layer) == row_key]
        if len(hits) != 1:
            raise ValueError(f'Frozen provenance row is not unique: {path.name}:{row_key}')
        return hits[0].get('status') or hits[0].get('test_status') or ''

    def _lookup_evidence(self, request, manifest):
        registry, _ = self._frozen_path('integration/evidence_registry.v2.jsonl', manifest)
        run_version = read_json(self._frozen_path('run.json', manifest)[0])['fingerprint']
        layer = ANALYSIS_LAYERS[request['analysis_type']]
        target = request['target_omics']
        cohort = ('external_atlas' if layer == 'scrna_context' else
                  self.cfg['analysis']['integration']['primary_cohort'])
        wanted = set(request['features'])
        validator = evidence_v2_validator(self.evidence_schema)
        ids, rows, statuses, cache = [], [], [], {}
        with registry.open(encoding='utf-8') as handle:
            for line in handle:
                record = json.loads(line)
                source = record.get('source', {})
                if source.get('evidence_layer') != layer or source.get('cohort') != cohort:
                    continue
                if layer == 'de' and source.get('omics') != target:
                    continue
                if record.get('biological_entity', {}).get('entity_id') not in wanted:
                    continue
                validator.validate(record)
                if record['provenance']['analysis_version'] != run_version:
                    raise ValueError('Evidence analysis_version differs from frozen run fingerprint')
                eid = record['evidence_id']
                if eid in ids:
                    raise ValueError(f'Duplicate frozen evidence_id: {eid}')
                provenance = record['provenance']
                relative = provenance['artifact_path']
                path, digest = self._frozen_path(relative, manifest)
                row_key = provenance['row_key']
                source_status = self._source_status(path, row_key, layer, cache)
                ids.append(eid)
                rows.append({'artifact_path': relative, 'sha256': digest, 'row_key': row_key})
                statuses.append(source_status)
        if not ids:
            return self._result(request, 'not_estimable', 'matching_result_absent')
        if all(s and s != 'tested' for s in statuses):
            return self._result(request, 'not_estimable', 'source_not_estimable', ids, rows)
        return self._result(request, 'no_new_evidence', 'existing_evidence_only', ids, rows)

    def _lookup_pathways(self, request, manifest):
        wanted = set(request['pathways'])
        rows, statuses = [], []
        cohort = self.cfg['analysis']['integration']['primary_cohort']
        paths = (f'pathways/transcriptomics/{cohort}/gsea_full.csv',
                 f'pathways/proteomics/{cohort}/gsea_full.csv',
                 'pathways/metabolomics/kegg_ora_full.csv')
        for relative in paths:
            if relative not in manifest:
                continue  # a frozen branch may have been unavailable
            path, digest = self._frozen_path(relative, manifest)
            with path.open(newline='', encoding='utf-8') as handle:
                for row in csv.DictReader(handle):
                    if row.get('pathway') in wanted:
                        rows.append({'artifact_path': relative, 'sha256': digest,
                                     'row_key': row['pathway']})
                        statuses.append(row.get('status') or
                                        ('tested' if row.get('padj') or row.get('pval') else 'not_estimable'))
        if not rows:
            return self._result(request, 'not_estimable', 'matching_result_absent')
        if all(s != 'tested' for s in statuses):
            return self._result(request, 'not_estimable', 'source_not_estimable', rows=rows)
        return self._result(request, 'no_new_evidence', 'existing_pathway_result_only', rows=rows)

    def execute(self, request, history=()):
        """Return a validated status and references; never run code or write artifacts."""
        if not isinstance(request, dict):
            request = {}
        if list(self.request_validator.iter_errors(request)):
            return self._result(request, 'unsupported', 'invalid_request')
        reason = self._check_history(request, history)
        if reason:
            return self._result(request, 'unsupported', reason)
        manifest = self._manifest()
        snapshot = read_json(self._frozen_path('run.json', manifest)[0])
        if snapshot.get('config') != self.cfg:
            raise ValueError('Planner config differs from frozen run config')
        if self.hypothesis_ids is None:
            hypotheses, _ = self._frozen_path('report/agent/hypotheses_agent.json', manifest)
            known = {h['id'] for h in read_json(hypotheses).get('hypotheses', [])}
        else:
            known = self.hypothesis_ids
        if request['trigger_hypothesis_id'] not in known:
            return self._result(request, 'unsupported', 'unknown_hypothesis')
        if request['analysis_type'] == 'pathway_result_lookup':
            return self._lookup_pathways(request, manifest)
        return self._lookup_evidence(request, manifest)

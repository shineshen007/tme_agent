"""Synthetic pre-plan receipts for unrelated legacy lifecycle tests (no retrieval claims)."""
import copy
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from orchestrator.research_literature import _digest, _passage, _seal
from multiomics_tui.planning_evidence import DESIGN_KEYS, PROTOCOL, _write, _configuration

DOC = 'Return True if x is neither an infinity nor a NaN, and False otherwise.'
PAPER = 'This synthetic methodology fixture describes the method and its limitations.'


def candidate():
    return {'id': 'finite', 'name': 'Synthetic fixture projection', 'language': 'python',
            'package': 'stdlib', 'apis': ['math.isfinite'], 'query': 'finite methodological fixture'}


def environment(root=None, candidates=None, cfg=None, folder=None):
    chosen = candidates or [candidate()]
    return [{'id': c['id'], 'language': c['language'], 'package': c['package'],
             'version': '3.14.7', 'runtime': '3.14.7', 'interpreter': sys.executable,
             'location': sys.prefix, 'status': 'verified',
             'apis': {api: {'signature': '(x, /)', 'documentation': DOC} for api in c['apis']}} for c in chosen]


class Literature:
    def __init__(self, *, empty=False, status='complete'):
        self.calls = []
        self.empty, self.status = empty, status

    def search(self, queries, context=None):
        self.calls.append(queries)
        record = _seal({'source_id': 'PMID:123456', 'pmid': '123456', 'publication_types': ['Journal Article'],
                        'passages': [_passage('abstract:1', PAPER)]})
        return {'status': self.status, 'records': [] if self.empty else [record], 'queries': queries,
                'errors': [] if self.status == 'complete' else [{'error': 'Synthetic provider failure'}]}


def binding():
    return {'id': 'finite', 'rationale': 'Synthetic plumbing fixture, not a scientific method acceptance.',
            'limitations': 'No live model or literature acceptance.', 'alternatives': 'No competing method in this fixture.',
            'documentation_quote': DOC, 'paper': {'source_id': 'PMID:123456', 'quote': PAPER},
            'requirements': {key: 'Explicit synthetic fixture requirement' for key in DESIGN_KEYS}}


def proposal():
    return {'kind': 'analysis', 'design': dict.fromkeys(DESIGN_KEYS, 'not_applicable') |
            {'analysis_unit': 'patient', 'data_scale': 'continuous', 'pairing': 'independent'},
            'design_basis': dict.fromkeys(DESIGN_KEYS, 'not_applicable') |
            {'analysis_unit': 'patient', 'data_scale': 'continuous', 'pairing': 'independent'},
            'candidates': [candidate()]}


def attach(root, plan, cfg=None, request=None):
    """Create an explicitly synthetic evidence bundle for a trusted test fixture.

    No production verifier is disabled. Registry transport, environment probes and
    provenance behavior are exercised independently in test_planning_evidence.py.
    """
    from multiomics_tui.llm import load_tui_config
    if cfg is None:
        cfg = load_tui_config(root) if (root / 'config/study_config.yaml').is_file() else {}
    request = plan['goal'] if request is None else request
    raw = copy.deepcopy(plan)
    inputs = raw['inputs']
    profiles = [dict(i) for i in inputs]
    bundle = {'protocol': PROTOCOL, 'context': {'request_sha256': _digest(request.encode()),
              'inspection': {'profiles': profiles},
              'configuration': _configuration(cfg)},
              'proposal': proposal(), 'methods': [{'candidate': candidate(), 'environment': environment()[0],
              'software': {'documentation': DOC, 'snapshots': [], 'metadata': {}, 'version': '3.14.7',
                           'registry_url': 'https://docs.python.org/3/library/math.html'},
              'literature': Literature().search(['fixture'])}], 'errors': [],
              'retrieved_at': datetime.now(timezone.utc).isoformat(), 'cache_hit': False}
    bundle['proposal']['design']['analysis_unit'] = plan.get('analysis_unit', 'patient')
    if raw.get('pathway_protocol'):
        c = {'id': 'gsva_protocol', 'name': 'Synthetic GSVA protocol fixture', 'language': 'r', 'package': 'GSVA',
             'apis': ['gsva'] + [v.split('::')[-1] for v in raw['pathway_protocol']['param_api'].values()],
             'query': 'GSVA method fixture'}
        bundle['proposal']['candidates'].append(c)
        row = copy.deepcopy(bundle['methods'][0])
        row.update(candidate=c, environment=environment(candidates=[c])[0])
        bundle['methods'].append(row)
    # Fixture plans do not claim inference. Their independent structure is explicit.
    import uuid
    ref = _write(root, '.runtime/planning_evidence/planning/attempt_' + uuid.uuid4().hex + '/bundle.json', bundle)
    raw['planning_protocol'] = PROTOCOL
    raw['planning_evidence'] = {'protocol': PROTOCOL, **ref, 'cache_days': 30, 'cache_hit': False}
    raw['method_bindings'] = [binding()]
    if raw.get('pathway_protocol'):
        raw['method_bindings'].append({**binding(), 'id': 'gsva_protocol'})
    for step in raw['steps']:
        step['method_ids'] = [b['id'] for b in raw['method_bindings']]
    raw['parameters'] = [p for p in raw.get('parameters', []) if p['name'] != 'planning_protocol'] + [
        {'name': 'planning_protocol', 'value': PROTOCOL, 'source': 'evidence:' + ref['sha256']}]
    return raw

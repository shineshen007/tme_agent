"""Scientific knowledge lifecycle and integrity tests; no model or network."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
import sqlite3

import pytest

from orchestrator.knowledge import (
    KnowledgeService, KnowledgeError, KnowledgeConflictError,
    KnowledgeIntegrityError, content_sha256, validate_payload,
)


def literature_payload(title='GENE1 in test disease'):
    text = 'A laboratory study reported increased GENE1 expression in the test model.'
    return {
        'title': title, 'kind': 'literature_report',
        'statement': 'GENE1 expression was increased in the reported test model.',
        'context': {'species': 'human', 'disease': 'test disease'},
        'sources': [{'source_id': 'pubmed:123', 'source_type': 'publication',
                     'locator': 'https://pubmed.ncbi.nlm.nih.gov/123/',
                     'sha256': hashlib.sha256(text.encode()).hexdigest(),
                     'text': text, 'quote': 'increased GENE1 expression', 'primary': True,
                     'metadata': {'content_scope': 'abstract'}}],
        'entities': [{'id': 'gene:9606:GENE1', 'type': 'gene', 'label': 'GENE1', 'aliases': ['OLD1']}],
        'relations': [],
    }


def approve(service, value):
    proposal = service.propose(value)
    return service.review(proposal['id'], proposal['sha256'], 'approve')


def test_construction_and_read_only_queries_do_not_create_library(tmp_path):
    root, shared = tmp_path / 'absent', tmp_path / 'shared'
    service = KnowledgeService(root, shared, read_only=True)
    assert service.search('GENE1') == []
    assert service.get('kb_missing') is None
    assert service.pending() == []
    assert service.snapshot('GENE1')['entries'] == []
    assert not root.exists() and not shared.exists()
    with pytest.raises(PermissionError):
        service.propose(literature_payload())
    assert not root.exists()


def test_proposal_review_hash_binding_and_rejection(tmp_path):
    service = KnowledgeService(tmp_path)
    proposal = service.propose(literature_payload())
    assert proposal['id'] and proposal['status'] == 'pending'
    assert service.search('GENE1') == []
    assert service.get(proposal['entry_id']) is None
    assert len(service.pending()) == 1
    assert service.propose(literature_payload()) == proposal
    with pytest.raises(KnowledgeConflictError):
        service.review(proposal['id'], '0' * 64, 'approve')
    reviewed = service.review(proposal['id'], proposal['sha256'], 'approve')
    assert reviewed['revision'] == 1 and reviewed['scope'] == 'project'
    assert reviewed['type'] == 'literature_report'
    assert service.search('OLD1')[0]['entry_id'] == reviewed['entry_id']
    assert service.pending() == []
    assert service.propose(literature_payload())['status'] == 'already_reviewed'
    with pytest.raises(KnowledgeConflictError):
        service.review(proposal['id'], proposal['sha256'], 'approve')
    rejected = service.propose(literature_payload('Other claim'))
    service.review(rejected['id'], rejected['sha256'], 'reject')
    assert len(service.search('GENE1')) == 1


@pytest.mark.parametrize('change,match', [
    (lambda p: p['sources'][0].update(quote='a nonexistent quote'), 'quote'),
    (lambda p: p['sources'][0].update(sha256='0' * 64), 'SHA256'),
    (lambda p: p['sources'][0].pop('quote'), 'text and quote'),
    (lambda p: p['sources'][0]['metadata'].pop('content_scope'), 'content_scope'),
    (lambda p: p['sources'][0].update(source_type='generated_report'), 'primary'),
    (lambda p: p['sources'][0]['metadata'].update(derived=True), 'primary'),
    (lambda p: p.update(sources=[]), 'primary source'),
    (lambda p: p.update(kind='statistical_fact'), 'primary source'),
    (lambda p: p.update(context={'effect': float('nan')}), 'finite JSON'),
    (lambda p: p.update(reviewed=True), 'Unknown payload'),
    (lambda p: p['entities'].append(deepcopy(p['entities'][0])), 'Duplicate entity'),
    (lambda p: p['relations'].append({'subject': 'gene:9606:GENE1', 'predicate': 'supports',
                                    'object': 'pathway:P1', 'source_ids': ['missing']}), 'source_ids'),
])
def test_invalid_evidence_cannot_enter_proposal_queue(tmp_path, change, match):
    payload = literature_payload()
    change(payload)
    with pytest.raises(KnowledgeError, match=match):
        KnowledgeService(tmp_path / 'wiki').propose(payload)
    assert not (tmp_path / 'wiki').exists()


def test_generated_sources_can_only_be_secondary_to_inference(tmp_path):
    payload = literature_payload()
    payload['kind'] = 'biological_inference'
    payload['sources'][0].update(source_type='generated_report', primary=False)
    item = approve(KnowledgeService(tmp_path), payload)
    assert item['kind'] == 'biological_inference'


def test_revision_history_and_concurrent_proposal_conflict(tmp_path):
    service = KnowledgeService(tmp_path)
    first = approve(service, literature_payload())
    v2 = literature_payload()
    v2.update(entry_id=first['entry_id'], base_sha256=first['sha256'], statement='A revised cautious statement.')
    proposal = service.propose(v2)
    competing = deepcopy(v2)
    competing['statement'] = 'A different revised statement.'
    competing_proposal = service.propose(competing)
    second = service.review(proposal['id'], proposal['sha256'], 'approve')
    assert second['revision'] == 2
    assert service.get(first['entry_id'], revision=1)['statement'] == first['statement']
    assert second['sha256'] != first['sha256']
    with pytest.raises(KnowledgeConflictError):
        service.review(competing_proposal['id'], competing_proposal['sha256'], 'approve')
    with pytest.raises(KnowledgeConflictError):
        service.propose(v2)
    with sqlite3.connect(tmp_path / 'knowledge.sqlite3') as db:
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            db.execute("UPDATE revisions SET payload='{}'")
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            db.execute('DELETE FROM revisions')


def test_conflicting_claims_and_bidirectional_links_are_retained(tmp_path):
    service = KnowledgeService(tmp_path)
    first = approve(service, literature_payload())
    second_payload = literature_payload('Contradictory GENE1 study')
    second_payload['statement'] = 'A different study contradicts the proposed mechanism.'
    second_payload['relations'] = [{'subject': 'gene:9606:GENE1', 'predicate': 'contradicts',
                                    'object': first['entry_id'], 'source_ids': ['pubmed:123']}]
    second = approve(service, second_payload)
    hits = service.search('GENE1')
    assert len(hits) == 2
    back = service.get(first['entry_id'])['backlinks']
    assert back[0]['predicate'] == 'contradicts'
    assert back[0]['entry_id'] == second['entry_id']
    assert service.search('GENE1', {'species': 'mouse'}) == []
    assert len(service.search('GENE1', {'species': 'human'})) == 2


def test_retraction_preserves_history_and_invalidates_pending_candidates(tmp_path):
    service = KnowledgeService(tmp_path)
    first = approve(service, literature_payload())
    edit = literature_payload()
    edit.update(entry_id=first['entry_id'], base_sha256=first['sha256'], statement='Proposed update.')
    proposal = service.propose(edit)
    with pytest.raises(KnowledgeConflictError):
        service.retract(first['entry_id'], 'f' * 64)
    withdrawn = service.retract(first['entry_id'], first['sha256'])
    assert withdrawn['status'] == 'retracted'
    assert service.search('GENE1') == []
    assert service.get(first['entry_id'], revision=1)['sha256'] == first['sha256']
    assert service.pending() == []
    with pytest.raises(KnowledgeConflictError):
        service.review(proposal['id'], proposal['sha256'], 'approve')


def test_explicit_shared_publication_scope_and_withdrawal(tmp_path):
    shared = tmp_path / 'shared'
    service = KnowledgeService(tmp_path / 'project_a', shared)
    value = literature_payload()
    value['shareable'] = True
    entry = approve(service, value)
    with pytest.raises(KnowledgeError, match='confirmation'):
        service.publish(entry['entry_id'], entry['sha256'])
    assert not shared.exists()
    published = service.publish(entry['entry_id'], entry['sha256'], confirmed=True)
    assert published['scope'] == 'shared'
    assert service.publish(entry['entry_id'], entry['sha256'], confirmed=True)['revision'] == 1
    isolated = KnowledgeService(tmp_path / 'project_b')
    assert isolated.search('GENE1') == []
    reader = KnowledgeService(tmp_path / 'project_b', shared, read_only=True)
    assert reader.search('GENE1')[0]['scope'] == 'shared'
    assert reader.get('shared:' + entry['entry_id'])['sha256'] == entry['sha256']
    assert not (tmp_path / 'project_b').exists()
    service.retract(entry['entry_id'], entry['sha256'])
    assert reader.search('GENE1') == []
    assert reader.get(entry['entry_id'])['status'] == 'retracted'


@pytest.mark.parametrize('change', [
    lambda p: p.update(shareable=False),
    lambda p: p['context'].update(patient_id='P1'),
    lambda p: p['context'].update(note='/Users/research/private.csv'),
    lambda p: p['context'].update(note='TCGA-DD-A4NB-01A'),
    lambda p: p['sources'][0].update(locator='file:///private/input.txt'),
    lambda p: p['sources'][0].update(locator='http://localhost/input.txt'),
    lambda p: p['sources'][0].update(locator='https://example.org/paper?token=secret'),
    lambda p: p.update(kind='statistical_fact', sources=[{
        'source_id': 'run:1', 'source_type': 'frozen_artifact', 'primary': True,
        'locator': 'results/private.csv', 'sha256': 'a' * 64}]),
])
def test_publication_rejects_private_content_without_changing_project(tmp_path, change):
    service = KnowledgeService(tmp_path / 'project', tmp_path / 'shared')
    value = literature_payload()
    value['shareable'] = True
    change(value)
    entry = approve(service, value)
    with pytest.raises(KnowledgeError):
        service.publish(entry['entry_id'], entry['sha256'], confirmed=True)
    assert service.get(entry['entry_id'])['status'] == 'reviewed'
    assert not (tmp_path / 'shared').exists()


def test_shared_experience_requires_deidentification_attestation(tmp_path):
    value = literature_payload('Method failure experience')
    value.update(kind='experience', shareable=True)
    value['sources'][0].update(source_type='experiment_record', locator='experience:method_failure')
    value['sources'][0]['metadata']['deidentified'] = True
    service = KnowledgeService(tmp_path / 'project', tmp_path / 'shared')
    entry = approve(service, value)
    with pytest.raises(KnowledgeError, match='deidentification'):
        service.publish(entry['entry_id'], entry['sha256'], confirmed=True)
    value.update(entry_id=entry['entry_id'], base_sha256=entry['sha256'],
                 sharing_review={'deidentified': True, 'reviewer': 'user'})
    reviewed = approve(service, value)
    assert service.publish(reviewed['entry_id'], reviewed['sha256'], confirmed=True)['scope'] == 'shared'


def test_parallel_proposals_and_review_are_serialized(tmp_path):
    def propose_one(_):
        return KnowledgeService(tmp_path).propose(literature_payload())
    with ThreadPoolExecutor(max_workers=4) as pool:
        proposals = list(pool.map(propose_one, range(12)))
    assert len({p['id'] for p in proposals}) == 1
    proposal = proposals[0]
    def review_one(_):
        try:
            KnowledgeService(tmp_path).review(proposal['id'], proposal['sha256'], 'approve')
            return 'approved'
        except KnowledgeConflictError:
            return 'conflict'
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(review_one, range(4)))
    assert results.count('approved') == 1
    assert results.count('conflict') == 3
    assert KnowledgeService(tmp_path).get(proposal['entry_id'])['revision'] == 1


def test_snapshot_is_stable_and_old_export_survives_new_revision(tmp_path):
    service = KnowledgeService(tmp_path / 'wiki')
    first = approve(service, literature_payload())
    frozen = service.snapshot('GENE1')
    assert frozen == service.snapshot('GENE1')
    assert frozen['sha256'] == content_sha256({k: v for k, v in frozen.items() if k not in {'sha256', 'markdown'}})
    destination = tmp_path / 'export'
    service.export_snapshot(destination, 'GENE1')
    saved = (destination / 'snapshot.json').read_bytes()
    assert json.loads(saved)['sha256'] == frozen['sha256']
    assert 'Sources:' in (destination / 'snapshot.md').read_text()
    with pytest.raises(FileExistsError):
        service.export_snapshot(destination, 'GENE1')
    value = literature_payload()
    value.update(entry_id=first['entry_id'], base_sha256=first['sha256'], statement='Updated interpretation.')
    approve(service, value)
    assert service.snapshot('GENE1')['sha256'] != frozen['sha256']
    assert (destination / 'snapshot.json').read_bytes() == saved


def test_corrupt_database_fails_closed(tmp_path):
    service = KnowledgeService(tmp_path)
    approve(service, literature_payload())
    with sqlite3.connect(tmp_path / 'knowledge.sqlite3') as db:
        db.execute('DROP TRIGGER immutable_revisions_update')
        db.execute("UPDATE revisions SET sha256=?", ('0' * 64,))
    with pytest.raises(KnowledgeIntegrityError):
        service.search('GENE1')


def test_schema_and_api_boundaries(tmp_path):
    with pytest.raises(KnowledgeError, match='different'):
        KnowledgeService(tmp_path, tmp_path)
    service = KnowledgeService(tmp_path)
    for bad_limit in (0, -1, 101, True):
        with pytest.raises(KnowledgeError):
            service.search('', limit=bad_limit)
    with pytest.raises(KnowledgeError):
        service.get('missing', revision=0)
    with pytest.raises(KnowledgeError):
        service.search('GENE1', context=[])
    with pytest.raises(KnowledgeError):
        validate_payload({'title': 'Incomplete'})


def test_snapshot_does_not_apply_interactive_result_limit(tmp_path):
    service = KnowledgeService(tmp_path)
    for i in range(101):
        approve(service, literature_payload(f'GENE1 evidence {i}'))
    assert len(service.search('GENE1')) == 8
    assert len(service.snapshot('GENE1')['entries']) == 101


def test_shared_and_project_copies_do_not_duplicate_evidence(tmp_path):
    service = KnowledgeService(tmp_path / 'local', tmp_path / 'shared')
    value = literature_payload()
    value['shareable'] = True
    entry = approve(service, value)
    service.publish(entry['entry_id'], entry['sha256'], confirmed=True)
    assert len(service.search('GENE1')) == 1
    assert service.search('GENE1')[0]['scope'] == 'project'

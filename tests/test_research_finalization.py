"""Audit failures cannot erase statistical success or leave a running research lease."""
import json
import hashlib
from pathlib import Path

import pytest

from orchestrator import research_pipeline
from test_research_pipeline import completed_task, frozen_analysis, run, StubClient


@pytest.mark.parametrize('filename', ['model_calls.json', 'research_manifest.json'])
def test_audit_write_failure_records_blocked_state(tmp_path, monkeypatch, filename):
    store, task_id, folder = completed_task(tmp_path)
    before = frozen_analysis(folder)
    original = research_pipeline.write_immutable_json

    def fail_audit(path, value):
        if Path(path).name == filename:
            raise OSError('injected audit failure')
        return original(path, value)

    monkeypatch.setattr(research_pipeline, 'write_immutable_json', fail_audit)
    with pytest.raises(OSError, match='injected audit failure'):
        run(tmp_path, task_id)
    state = store.get(task_id)
    assert state['status'] == 'succeeded'
    assert state['research_status'] == 'blocked'
    assert state['scientific_status'] == 'NOT_EVALUABLE'
    assert 'finalize immutable research audit' in state['research_error']
    assert not state.get('research_attempts')
    assert frozen_analysis(folder) == before


@pytest.mark.parametrize('corrupt', [False, True])
def test_missing_or_corrupt_external_call_audit_blocks(tmp_path, corrupt):
    store, task_id, folder = completed_task(tmp_path)
    before = frozen_analysis(folder)
    client = StubClient()
    client.audit_dir = tmp_path / 'call_audit'
    client.audit_dir.mkdir()

    def number_call(role, content):
        client.calls[-1]['call'] = len(client.calls)
        if corrupt:
            (client.audit_dir / f'{len(client.calls):03d}_{role}.json').write_text('{corrupt')

    client.on_call = number_call
    with pytest.raises((ValueError, json.JSONDecodeError)):
        run(tmp_path, task_id, client)
    assert store.get(task_id)['research_status'] == 'blocked'
    assert store.get(task_id)['status'] == 'succeeded'
    assert not store.get(task_id).get('research_attempts')
    assert frozen_analysis(folder) == before


def test_critic_rejection_does_not_report_scientific_completion(tmp_path):
    store, task_id, _ = completed_task(tmp_path)
    result = run(tmp_path, task_id, StubClient(critic_decision='block'))
    assert result['research_status'] == 'blocked'
    assert result['scientific_status'] == 'NOT_EVALUABLE'
    assert result['error'] == 'critic_blocked'
    report = (Path(result['attempt']) / 'research_report.md').read_text()
    assert '**Scientific status: NOT_EVALUABLE**' in report
    assert store.get(task_id)['status'] == 'succeeded'


def test_pre_attempt_source_failure_blocks_the_retry_state(tmp_path):
    store, task_id, folder = completed_task(tmp_path)
    first = run(tmp_path, task_id)
    original_receipts = store.get(task_id)['research_attempts']
    (folder / 'tables/transcriptomics.csv').write_text('tampered output\n')
    with pytest.raises(ValueError, match='hash mismatch'):
        run(tmp_path, task_id)
    state = store.get(task_id)
    assert state['status'] == 'succeeded' and state['research_status'] == 'blocked'
    assert state['scientific_status'] == 'NOT_EVALUABLE'
    assert state['research_attempts'] == original_receipts
    assert (Path(first['attempt']) / 'research_manifest.json').is_file()


@pytest.mark.parametrize('source_text,quoted', [
    ('Expression  changes\naccompanied composition.', 'Expression changes accompanied composition.'),
    ('A ﬁnding &amp; mechanism.', 'A finding & mechanism.')])
def test_knowledge_candidates_preserve_multi_passage_conflicts_and_original_quotes(tmp_path, source_text, quoted):
    from orchestrator.research_context import TaskResearchAdapter
    from test_research_pipeline import CFG
    from orchestrator.knowledge_context import knowledge_service
    _, task_id, folder = completed_task(tmp_path)
    context = TaskResearchAdapter(tmp_path, task_id).load()
    attempt = folder / 'research/quote_fixture'
    attempt.mkdir(parents=True)
    statements = [('abstract:1', source_text, quoted, 'supports'),
                  ('abstract:2', 'The alternative model disagreed.', 'alternative model disagreed', 'contradicts')]
    record = {'source_id': 'PMID:123', 'pmid': '123', 'url': 'https://pubmed.ncbi.nlm.nih.gov/123/',
              'passages': [{'locator': locator, 'text': text, 'kind': 'abstract',
                            'sha256': hashlib.sha256(text.encode()).hexdigest()}
                           for locator, text, _, _ in statements]}
    draft = {'interpretations': [{'id': 'I1', 'kind': 'literature_report',
                                 'statement': 'The synthetic studies reported conflicting expression findings.'}],
             'hypotheses': [], 'literature_claims': [
                 {'claim_id': 'I1', 'source_id': 'PMID:123', 'locator': locator,
                  'quote': quote, 'relation': relation, 'directness': 'indirect'}
                 for locator, _, quote, relation in statements]}
    (attempt / 'synthesis.json').write_text(json.dumps(draft))
    result = research_pipeline._proposals(tmp_path, context, draft, {'records': [record]}, attempt, CFG)
    assert result['status'] == 'pending_human_review' and not result['errors']
    candidate = knowledge_service(tmp_path, CFG['knowledge']).pending()[0]['payload']
    sources = [source for source in candidate['sources'] if source['source_type'] == 'publication']
    assert len({source['source_id'] for source in sources}) == 2
    assert {source['metadata']['relation'] for source in sources} == {'supports', 'contradicts'}
    assert {source['metadata']['document_source_id'] for source in sources} == {'PMID:123'}
    assert sources[0]['text'] == source_text
    assert all(source['quote'] in source['text'] for source in sources)
    assert all(hashlib.sha256(source['text'].encode()).hexdigest() == source['sha256'] for source in sources)

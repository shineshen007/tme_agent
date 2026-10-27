"""Offline provider, provenance, quotation and failure-state contracts."""
from copy import deepcopy
import hashlib
import json
import urllib.error
import urllib.parse

import pytest

from orchestrator.research_literature import (
    LiteratureService, _digest, _merge_records, _passage, _seal,
    select_passages, validate_literature_claims, verify_source_record,
)


PUBMED_XML = '''<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>12345</PMID>
<Article><ArticleTitle>Lactate &amp; immune context</ArticleTitle>
<Journal><Title>Fixture Journal</Title><JournalIssue><PubDate><Year>2024</Year></PubDate></JournalIssue></Journal>
<Abstract><AbstractText Label="BACKGROUND">Background description.</AbstractText>
<AbstractText Label="RESULTS">Lactate reduced T cell activation in this mouse model.</AbstractText></Abstract>
<AuthorList><Author><ForeName>A</ForeName><LastName>Researcher</LastName></Author></AuthorList>
<PublicationTypeList><PublicationType>Journal Article</PublicationType><PublicationType>Retracted Publication</PublicationType></PublicationTypeList>
</Article><CommentsCorrectionsList><CommentsCorrections RefType="RetractionIn"><PMID>54321</PMID><RefSource>Correction reference</RefSource></CommentsCorrections></CommentsCorrectionsList>
</MedlineCitation><PubmedData><ArticleIdList><ArticleId IdType="pubmed">12345</ArticleId>
<ArticleId IdType="doi">10.1000/EXAMPLE</ArticleId><ArticleId IdType="pmc">PMC12345</ArticleId>
</ArticleIdList></PubmedData></PubmedArticle></PubmedArticleSet>'''

EUROPE_JSON = {'hitCount': 1, 'resultList': {'result': [
    {'id': '12345', 'source': 'MED', 'pmid': '12345', 'pmcid': 'PMC12345',
     'doi': '10.1000/example', 'title': 'Lactate and immune context', 'pubYear': '2024',
     'abstractText': 'Lactate reduced T cell activation in this mouse model.',
     'journalInfo': {'journal': {'title': 'Fixture Journal'}},
     'authorList': {'author': [{'fullName': 'A Researcher'}]},
     'pubTypeList': {'pubType': ['Journal Article']}, 'isRetracted': 'Y', 'isOpenAccess': 'Y'}]}}


class Transport:
    def __init__(self):
        self.calls = []
        self.fail_provider = None
        self.full_text = '<article><front><permissions><license>CC BY</license></permissions></front><body><sec><p>A perturbation changed the measured endpoint.</p></sec></body></article>'
        self.epmc = deepcopy(EUROPE_JSON)
        self.xml = PUBMED_XML
        self.search = {'esearchresult': {'count': '1', 'idlist': ['12345']}}

    def __call__(self, url, timeout):
        self.calls.append((url, timeout))
        if self.fail_provider and self.fail_provider in url:
            raise TimeoutError('fixture unavailable')
        if 'esearch.fcgi' in url:
            return json.dumps(self.search).encode()
        if 'efetch.fcgi' in url:
            return self.xml.encode()
        if 'fullTextXML' in url:
            if isinstance(self.full_text, Exception):
                raise self.full_text
            return self.full_text.encode()
        return json.dumps(self.epmc).encode()


def service(tmp_path, transport=None, **settings):
    return LiteratureService(tmp_path / 'literature',
                             {'min_interval': 0, 'retries': 0, **settings}, transport or Transport())


def test_provider_merge_retains_identifiers_sources_and_retraction(tmp_path):
    transport = Transport()
    result = service(tmp_path, transport, fetch_full_text=True).search(['lactate T cell'])
    assert result['status'] == 'complete'
    assert len(result['records']) == 1
    r = result['records'][0]
    assert r['source_id'] == 'PMID:12345'
    assert (r['pmid'], r['pmcid'], r['doi'], r['year']) == ('12345', 'PMC12345', '10.1000/example', '2024')
    assert set(r['providers']) == {'pubmed', 'europepmc'}
    assert 'Retracted Publication' in r['publication_types']
    assert any(x.get('relation') == 'RetractionIn' for x in r['retraction_metadata'])
    assert any(x.get('field') == 'isRetracted' for x in r['retraction_metadata'])
    assert any(p['kind'] == 'full_text' for p in r['passages'])
    verify_source_record(r)
    assert all(q['response_snapshots'] for q in result['queries'])
    assert result['snapshot_sha256'] == _digest({k: v for k, v in result.items() if k != 'snapshot_sha256'})
    assert len(transport.calls) == 4


def test_cache_and_refresh_preserve_immutable_snapshots(tmp_path):
    transport = Transport()
    first = service(tmp_path, transport, providers=['pubmed']).search(['lactate'])
    calls = len(transport.calls)
    cached = service(tmp_path, transport, providers=['pubmed']).search(['lactate'])
    assert len(transport.calls) == calls and cached['queries'][0]['cache_hit']
    transport.xml = PUBMED_XML.replace('Background description.', 'Revised background description.')
    updated = service(tmp_path, transport, providers=['pubmed'], refresh=True).search(['lactate'])
    assert len(transport.calls) == calls + 2
    assert updated['records'][0]['source_text_sha256'] != first['records'][0]['source_text_sha256']
    snapshots = tmp_path / 'literature/snapshots'
    assert (snapshots / (first['queries'][0]['snapshot_sha256'] + '.json')).is_file()
    assert (snapshots / (updated['queries'][0]['snapshot_sha256'] + '.json')).is_file()


def test_offline_returns_cached_sources_with_explicit_offline_state(tmp_path):
    transport = Transport()
    service(tmp_path, transport, providers=['pubmed']).search(['lactate'])
    count = len(transport.calls)
    result = service(tmp_path, transport, providers=['pubmed'], offline=True).search(['lactate', 'unknown term'])
    assert result['status'] == 'offline' and len(result['records']) == 1
    assert len(transport.calls) == count
    assert [q['status'] for q in result['queries']] == ['complete', 'offline']
    assert result['novelty_status'] == 'not_assessed'


def test_zero_results_do_not_establish_novelty(tmp_path):
    transport = Transport()
    transport.search = {'esearchresult': {'count': '0', 'idlist': []}}
    result = service(tmp_path, transport, providers=['pubmed']).search(['public rare mechanism'])
    assert result['status'] == 'no_results'
    assert result['novelty_status'] == 'not_assessed'
    assert result['queries'][0]['total_hits'] == 0


def test_partial_failure_keeps_successful_provider_records(tmp_path):
    transport = Transport()
    transport.fail_provider = 'www.ebi.ac.uk'
    result = service(tmp_path, transport).search(['lactate'])
    assert result['status'] == 'partial' and len(result['records']) == 1
    assert result['errors'][0]['provider'] == 'europepmc'
    assert [q['status'] for q in result['queries']] == ['complete', 'failed']


def test_all_failed_and_missing_metadata_are_not_complete(tmp_path):
    transport = Transport()
    transport.fail_provider = 'eutils.ncbi.nlm.nih.gov'
    assert service(tmp_path, transport, providers=['pubmed']).search(['lactate'])['status'] == 'failed'
    transport.fail_provider = None
    transport.xml = '<PubmedArticleSet/>'
    result = service(tmp_path, transport, providers=['pubmed']).search(['lactate'])
    assert result['status'] == 'partial'
    assert result['records'] == [] and result['queries'][0]['total_hits'] == 1


def test_optional_full_text_failure_is_partial_not_absence_of_evidence(tmp_path):
    transport = Transport()
    transport.full_text = urllib.error.HTTPError('fixture', 404, 'missing', {}, None)
    result = service(tmp_path, transport, providers=['europepmc'], fetch_full_text=True).search(['lactate'])
    assert result['status'] == 'partial' and result['records'][0]['abstract']
    assert result['errors'][0]['stage'] == 'full_text'


def test_non_open_access_never_fetches_full_text(tmp_path):
    transport = Transport()
    transport.epmc['resultList']['result'][0]['isOpenAccess'] = 'N'
    service(tmp_path, transport, providers=['europepmc'], fetch_full_text=True).search(['lactate'])
    assert len(transport.calls) == 1


@pytest.mark.parametrize('query', ['TCGA-AA-1234 tumor', 'sample:abc comparison', '/Users/person/raw.csv',
                                  'researcher@example.org', 'gene\tpatient\tvalue'])
def test_private_identifiers_or_raw_data_rejected_before_transport(tmp_path, query):
    transport = Transport()
    with pytest.raises(ValueError):
        service(tmp_path, transport).search([query])
    assert not transport.calls


def test_explicit_private_ids_and_matrix_context_rejected(tmp_path):
    transport = Transport()
    client = service(tmp_path, transport)
    with pytest.raises(ValueError):
        client.search(['custom-patient-001 lactate'], context={'private_ids': ['custom-patient-001']})
    with pytest.raises(ValueError):
        client.search(['lactate'], context={'matrix': [[1, 2]]})
    with pytest.raises(ValueError):
        client.search(['lactate'], context={'disease': 'custom-patient-001 HCC', 'private_ids': ['custom-patient-001']})
    assert not transport.calls


def test_public_context_does_not_leave_local_denylist_in_snapshot(tmp_path):
    result = service(tmp_path, providers=['pubmed']).search(
        ['lactate'], {'species': 'human', 'disease': 'HCC', 'private_ids': ['PRIVATE-123']})
    assert 'PRIVATE-123' not in json.dumps(result)
    assert result['context'] == {'species': 'human', 'disease': 'HCC'}


def test_response_retry_is_bounded_and_uses_timeout(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr('orchestrator.research_literature.time.sleep', lambda _: None)
    def transient(url, timeout):
        calls.append(timeout)
        if len(calls) < 3:
            raise urllib.error.HTTPError(url, 429, 'retry', {}, None)
        return json.dumps({'esearchresult': {'count': '0', 'idlist': []}})
    result = service(tmp_path, transient, providers=['pubmed'], retries=2, timeout=7).search(['lactate'])
    assert result['status'] == 'no_results' and calls == [7, 7, 7]


def test_cache_tampering_fails_closed_offline(tmp_path):
    service(tmp_path, providers=['pubmed']).search(['lactate'])
    cache = next((tmp_path / 'literature/queries').glob('*.json'))
    content = json.loads(cache.read_text())
    content['records'][0]['passages'][0]['text'] = 'Fabricated claim'
    cache.write_text(json.dumps(content))
    result = service(tmp_path, providers=['pubmed'], offline=True).search(['lactate'])
    assert result['status'] == 'offline' and result['records'] == []
    assert result['errors'][0]['stage'] == 'cache'


def test_raw_response_tampering_also_invalidates_cached_provenance(tmp_path):
    service(tmp_path, providers=['pubmed']).search(['lactate'])
    raw = next((tmp_path / 'literature/responses').glob('*.bin'))
    raw.write_bytes(b'changed content')
    result = service(tmp_path, providers=['pubmed'], offline=True).search(['lactate'])
    assert not result['records'] and result['errors'][0]['stage'] == 'cache'


def test_doi_and_pmid_aliases_merge_without_double_counting():
    first = _seal({'source_id': 'DOI:10.1/a', 'doi': '10.1/a', 'providers': ['europepmc'],
                   'passages': [_passage('epmc:1', 'A reported association.')]})
    second = _seal({'source_id': 'PMID:12345', 'pmid': '12345', 'providers': ['pubmed'],
                    'passages': [_passage('pubmed:1', 'A reported association.')]})
    bridge = _seal({'source_id': 'PMID:12345', 'pmid': '12345', 'doi': '10.1/a',
                    'providers': ['europepmc'], 'passages': []})
    merged = _merge_records([first, second, bridge])
    assert len(merged) == 1 and merged[0]['source_id'] == 'PMID:12345'
    assert len(merged[0]['passages']) == 2


def test_provider_cannot_exceed_per_query_record_bound(tmp_path):
    transport = Transport()
    transport.search = {'esearchresult': {'count': '2', 'idlist': ['12345', '12346']}}
    result = service(tmp_path, transport, providers=['pubmed'], max_records=1).search(['lactate'])
    assert result['status'] == 'failed' and len(transport.calls) == 1


def source_record():
    return _seal({'source_id': 'PMID:12345', 'pmid': '12345',
                  'passages': [_passage('abstract:1', 'Lactate reduced T cell activation in this mouse model.')]})


def claim(**changes):
    return {'claim_id': 'hypothesis-1', 'source_id': 'PMID:12345', 'relation': 'supports',
            'directness': 'indirect', 'quote': 'Lactate reduced T cell activation', **changes}


def test_valid_quote_has_source_hash_and_requires_semantic_review():
    record = source_record()
    result = validate_literature_claims([claim(quote='Lactate  reduced\nT cell activation')], [record], {'hypothesis-1'})
    assert result[0]['text_verified'] and result[0]['semantic_review'] == 'required'
    assert result[0]['locator'] == 'abstract:1'
    assert result[0]['source_text_sha256'] == record['source_text_sha256']


@pytest.mark.parametrize('change', [
    {'source_id': 'PMID:99999'}, {'quote': 'Lactate proved human cancer causality'},
    {'quote': ''}, {'locator': 'body:999'}, {'claim_id': 'invented'},
    {'relation': 'proves'}, {'directness': 'certain'}, {'source_text_sha256': 'wrong'},
    {'passage_sha256': 'wrong'},
])
def test_invalid_citations_cannot_pass_with_valid_pmid(change):
    with pytest.raises(ValueError):
        validate_literature_claims([claim(**change)], [source_record()], {'hypothesis-1'})


def test_unknown_claim_set_and_altered_source_rejected():
    with pytest.raises(ValueError):
        validate_literature_claims([claim()], [source_record()])
    record = source_record()
    record['passages'][0]['text'] += ' Fabricated extension.'
    with pytest.raises(ValueError, match='hash mismatch'):
        validate_literature_claims([claim()], [record], {'hypothesis-1'})


def test_relevant_passage_beyond_600_characters_is_selected():
    text = 'Unrelated background. ' * 60 + 'Lactate reduced T cell activation. ' + 'Other detail. ' * 30
    source = {'abstract': text}
    selected = select_passages(source, 'lactate T cell', limit_chars=120)
    assert 'Lactate reduced T cell activation.' in selected[0]['text']
    assert sum(len(p['text']) for p in selected) <= 120
    assert selected[0]['sha256'] == hashlib.sha256(' '.join(text.split()).encode()).hexdigest()


def test_long_unsplit_paragraph_uses_relevant_window():
    text = 'background ' * 120 + 'lactate target mechanism ' + 'other ' * 100
    selected = select_passages({'abstract': text}, 'lactate', limit_chars=100)
    assert 'lactate target mechanism' in selected[0]['text']


def test_legacy_stance_check_optionally_verifies_source_text():
    from orchestrator.checkers import _check_stances
    payload = {'literature_stances': [{'pmid': '12345', 'stance': 'supports', 'quote': 'invented statement'}]}
    _check_stances(payload, {'12345'})  # Existing caller compatibility.
    with pytest.raises(ValueError, match='does not match'):
        _check_stances(payload, {'12345'}, [source_record()])
    payload['literature_stances'][0]['quote'] = 'Lactate reduced T cell activation'
    _check_stances(payload, {'12345'}, [source_record()])


@pytest.mark.parametrize('failure', ['partial', 'exception'])
def test_legacy_additional_errors_persist_and_propagate(tmp_path, monkeypatch, failure):
    from orchestrator import literature
    folder = tmp_path / 'run/literature'
    folder.mkdir(parents=True)
    path = folder / 'records.json'
    path.write_text(json.dumps({'status': 'complete', 'records': [{'pmid': '12345'}], 'errors': []}))
    def invoke(*args):
        if failure == 'exception':
            raise TimeoutError('fixture unavailable')
        return {'status': 'partial', 'records': [], 'errors': [{'query': 'lactate', 'error': 'timeout'}]}
    monkeypatch.setattr(literature, '_invoke', invoke)
    cfg = {'literature': {'max_additional_queries': 2, 'retmax_per_query': 5, 'cache_dir': 'cache',
                          'eutils_base': 'https://example.org/', 'request_pause_seconds': 0}}
    result = literature.fetch_additional(tmp_path, tmp_path / 'run', cfg, [{'query': 'lactate'}])
    assert result['status'] == ('failed' if failure == 'exception' else 'partial')
    saved = json.loads(path.read_text())
    assert saved['status'] == 'partial' and saved['records'][0]['pmid'] == '12345'
    assert saved['errors']


def test_legacy_brief_retrieves_relevant_text_beyond_head(tmp_path):
    from orchestrator.agents import literature_brief
    folder = tmp_path / 'literature'
    folder.mkdir()
    (folder / 'records.json').write_text(json.dumps({'records': [{'axis': 'lactate', 'pmid': '12345',
        'title': 'Lactate context', 'abstract': 'General background. ' * 80 + 'Lactate changed activation.'}]}))
    result = literature_brief(tmp_path, {}, 2)
    assert 'Lactate changed activation.' in result
    assert 'SOURCE abstract:1' in result and 'SHA256' in result

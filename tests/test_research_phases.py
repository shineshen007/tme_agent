"""Realistic offline research phase contracts, including failure and refusal paths."""
from copy import deepcopy
import json

import jsonschema
import pytest

from orchestrator.agent_loop import AgentReasoningSession
from orchestrator.llm_client import LLMBudgetExceeded, LLMValidationError, PrivacyViolation
from orchestrator.research_context import ResearchContext
from orchestrator.research_literature import _passage, _seal
from orchestrator import research_phases as phases


EID = 'ev_' + 'a' * 20
SID = 'DOI:10.1000/example.2024'
KID = 'wiki_1e256'


def observation(eid=EID, omics='transcriptomics', layer='de', selected=True, padj=0.01,
                selection=None, entity_id='TP53', entity_type='gene', direction='up',
                status=None, symbol=None, pathway=None, cell_type=None):
    entity = {'entity_id': entity_id, 'symbol': entity_id if symbol is None else symbol,
              'entity_type': entity_type}
    if pathway is not None:
        entity['pathway'] = pathway
    if cell_type is not None:
        entity['cell_type'] = cell_type
    value = {'effect': 1.2, 'p_value': 0.005, 'adjusted_p': padj, 'n': 20,
             'selected': selected,
             'status': status if status is not None else ('available' if padj is not None else 'not_estimable'),
             'direction': direction, 'effect_scale': 'log2_fold_change', 'missing_fields': []}
    if selection is not None:
        value['selection'] = selection
    return {'evidence_id': eid,
            'source': {'omics': omics, 'evidence_layer': layer, 'cohort': 'local',
                       'evidence_context': 'measured_evidence' if layer == 'de' else 'derived_estimate',
                       'measured_omics': [omics] if layer == 'de' else []},
            'biological_entity': entity,
            'observation': value}


def frozen_selection(verified=True, selected=True):
    """Mirror the frozen selection contract supplied by ResearchContext."""
    return {'status': ('verified_selected' if verified and selected is True else
                       'verified_not_selected' if verified else 'not_verifiable'),
            'verified': verified, 'raw_selected': selected, 'selected': selected,
            'adjusted_p_max': 0.05, 'effect_min': None, 'effect_max': None,
            'analysis_unit_count': None,
            'reasons': [] if verified else ['no_frozen_selection_thresholds']}


def evidence_review(claim_id='claim_1e256', reviewed=(EID,), counter=(), negative=(),
                    assessment='consistent'):
    return {'claim_id': claim_id, 'reviewed_evidence_ids': list(reviewed),
            'counterevidence_ids': list(counter),
            'negative_result_ids': list(negative), 'assessment': assessment,
            'rationale': 'The independently reviewed counterevidence is consistent with the stated interpretation.'}


def interpretation(kind='biological_inference', **kwargs):
    return {'id': 'claim_1e256', 'kind': kind,
            'statement': 'TP53 abundance may reflect altered stress signaling.',
            'evidence_ids': [EID], 'knowledge_ids': [KID],
            'alternative_explanations': ['Cell composition could explain the association.'],
            'limitations': ['Transcript abundance does not establish protein activity.'], **kwargs}


def paper(metadata=None):
    return _seal({'source_id': SID, 'title': 'A cellular perturbation study', 'year': '2024',
                  'doi': '10.1000/example.2024', 'pmid': '12345', 'url': 'https://doi.org/10.1000/example.2024',
                  'publication_types': ['Journal Article'],
                  'retraction_metadata': metadata if metadata is not None else [{'field': 'isRetracted', 'value': 'N'}],
                  'passages': [_passage('full_text:2.3:1e256',
                       'The experiment included 12 biological replicates and reported altered stress signaling.', 'full_text')]})


def literature():
    return {'status': 'complete', 'records': [paper()], 'errors': [],
            'queries': [{'query': 'TP53 stress carcinoma', 'status': 'complete'}]}


def relation(claim_id='claim_1e256', **kwargs):
    return {'claim_id': claim_id, 'source_id': SID, 'relation': 'supports', 'directness': 'indirect',
            'quote': 'The experiment included 12 biological replicates', 'locator': 'full_text:2.3:1e256',
            'rationale': 'The source describes a related model, so extrapolation remains uncertain.',
            'context_match': {key: 'unknown' for key in ('species', 'disease', 'tissue', 'model', 'assay', 'outcome')},
            **kwargs}


def hypothesis():
    return {'id': 'hypothesis_1', 'kind': 'hypothesis',
            'statement': 'TP53 may mediate the stress response in the tumor context.',
            'evidence_ids': [EID], 'counterevidence_ids': [], 'knowledge_ids': [KID],
            'conditions': ['The relevant protein must be expressed in the model.'],
            'testable_predictions': [{'prediction': 'Target perturbation changes the stress readout.',
                'readout': 'Stress signaling and viability.', 'expected_if_true': 'The response changes after perturbation.',
                'expected_if_false': 'The response remains stable under an effective perturbation.'}],
            'validation_experiments': [{'hypothesis_id': 'hypothesis_1', 'model_system': 'HepG2 cells',
                'model_rationale': 'Use a liver tumor model after verifying target expression.',
                'perturbation': 'Target knockdown and a rescue construct.',
                'support_criterion': 'The readout changes and rescue restores the response.',
                'falsification_criterion': 'Effective knockdown does not alter the prespecified readout.',
                'replication_unit': 'Independent biological cultures.',
                'sample_size_basis': 'A pilot and an approved power analysis are needed.',
                'rescue_or_orthogonal': 'Use a rescue construct and an orthogonal readout.',
                'feasibility': 'Requires model and assay validation.', 'controls': ['Non-targeting control', 'Vehicle control'],
                'readouts': ['Stress signaling', 'Viability'], 'knowledge_ids': [KID], 'source_ids': [],
                'unknowns': ['Perturbation dose and duration require a sourced protocol.']}],
            'alternative_explanations': ['Cell composition may confound the original signal.'],
            'unknowns': ['Protein activity is not measured.']}


def draft(with_hypothesis=False, with_literature=False):
    return {'interpretations': [interpretation()], 'hypotheses': [hypothesis()] if with_hypothesis else [],
            'literature_claims': [relation()] if with_literature else [], 'novelty': [],
            'limitations': ['Observational analysis remains exploratory.']}


def review(pairs=(), reviews=None):
    return {'decision': 'accept', 'issues': [], 'analysis_requests': [], 'limitations': [],
            'literature_review': [{'claim_id': cid, 'source_id': sid, 'decision': 'supported_as_stated',
                                   'rationale': 'The stated indirect interpretation is consistent with the source context.'}
                                  for cid, sid in pairs],
            'evidence_review': list(reviews) if reviews is not None else [evidence_review()]}


class Client:
    def __init__(self, replies=None):
        self.replies = replies or {}
        self.calls = []

    def chat_json(self, **kwargs):
        self.calls.append(kwargs)
        phase = kwargs['schema_name'].removeprefix('research_').rsplit('_v', 1)[0]
        response = self.replies[phase]
        if isinstance(response, Exception):
            raise response
        response = deepcopy(response)
        kwargs['validator'](response)
        return {'parsed': response}


class Literature:
    def __init__(self, result=None):
        self.result = result if result is not None else literature()
        self.calls = []

    def search(self, queries, context=None):
        self.calls.append((queries, context))
        return deepcopy(self.result)


def session(tmp_path, replies=None, result=None):
    context = ResearchContext(question='Explain the liver tumor stress response.', disease='HCC', species='human',
                              tissue='liver', analysis_unit='patient', cohort_id='local', evidence=[observation()])
    value = AgentReasoningSession(tmp_path, tmp_path / 'attempt', {}, [], context=context,
            knowledge_context={'entries': [{'entry_id': KID, 'statement': 'Curated stress biology.'}]},
            client=Client(replies), literature_service=Literature(result))
    value.literature_result = deepcopy(result if result is not None else literature())
    return value


def test_normal_agent_session_full_phase_flow_accepts_ids_quotes_and_empty_hypotheses(tmp_path):
    responses = {'analyst': {'interpretations': [interpretation()], 'limitations': []},
                 'librarian': {'queries': ['TP53 HCC stress contradictory evidence']},
                 'integrator': {'clusters': [{'id': 'cluster_1e256', 'topic': 'Stress response',
                    'supporting_evidence_ids': [EID], 'counterevidence_ids': [],
                    'interpretation': 'The observations may reflect stress signaling.',
                    'missing_links': ['Protein activity is not measured.'], 'direction_note': 'RNA abundance is higher.'}]},
                 'reasoner': draft(with_literature=True), 'critic': review([('claim_1e256', SID)])}
    value = session(tmp_path, responses)
    analysts = value.analysts_and_librarian()
    clusters = value.integrate(analysts)
    raw, synthesis, rejected = value.reason(clusters)
    result = value.critic(synthesis, rejected)
    assert result['decision'] == 'accept' and synthesis['hypotheses'] == []
    assert synthesis['literature_claims'][0]['text_verified']
    assert synthesis['literature_claims'][0]['semantic_review'] == 'required'
    assert raw['literature_claims'][0]['quote'].startswith('The experiment included 12')
    assert value.context.evidence[0]['observation']['effect'] == 1.2
    assert not value.errors and len(value.client.calls) == 5
    payloads = [json.loads(c['user']) for c in value.client.calls]
    assert all('coverage' in payload for payload in payloads)
    for required in ('observations', 'targeted_observations', 'registry_summary', 'query_terms'):
        assert all(required in payload for payload in payloads)
    assert all(payload['registry_summary']['total'] == 1 for payload in payloads)


def test_empty_hypotheses_can_pass_actual_bounded_reasoning_graph(tmp_path):
    from orchestrator.loop_graph import run_context_reasoning
    value = session(tmp_path, {'integrator': {'clusters': []}, 'reasoner': draft(), 'critic': review()})
    value.research_analysts = {'interpretations': [], 'limitations': []}
    saved = {}
    result = run_context_reasoning(value, lambda: None, lambda path, payload: saved.update({path: payload}), maximum=1)
    assert result['action'] == 'accept' and result['draft']['hypotheses'] == []
    assert 'iteration_01/review.json' in saved


def test_hypothesis_model_names_pass_and_measured_omics_are_computed_from_sources(tmp_path):
    value = session(tmp_path, {'reasoner': draft(with_hypothesis=True)})
    _, synthesis, _ = value.reason({'clusters': []})
    assert synthesis['hypotheses'][0]['measured_omics'] == ['transcriptomics']
    assert synthesis['hypotheses'][0]['validation_status'] == 'not_independently_validated'
    assert synthesis['hypotheses'][0]['evidence_basis'] == 'single_omics_or_derived'


@pytest.mark.parametrize('statement', ['TP53 increased 1.5 fold.', 'The cohort contained 20 patients.',
                                     'The signal changed by 30%.', '使用三只小鼠验证。',
                                     'Use three biological samples.', 'Use five replicates.'])
def test_numeric_narrative_cannot_become_model_fact(tmp_path, statement):
    value = draft()
    value['interpretations'][0]['statement'] = statement
    with pytest.raises(ValueError, match='quantity'):
        phases.validate_draft(session(tmp_path), value)


def test_numeric_experiment_parameter_is_not_a_design_fact(tmp_path):
    value = draft(with_hypothesis=True)
    value['hypotheses'][0]['validation_experiments'][0]['perturbation'] = 'Apply treatment for 24 hours.'
    with pytest.raises(ValueError, match='quantity'):
        phases.validate_draft(session(tmp_path), value)


def test_known_disease_name_with_digit_is_not_a_quantity(tmp_path):
    value = session(tmp_path)
    value.context.disease = 'type 2 diabetes'
    content = draft()
    content['interpretations'][0]['statement'] = 'This association may reflect type 2 diabetes context.'
    phases.validate_draft(value, content)


@pytest.mark.parametrize('statement', ['The signal may be relevant. TP53 causes tumor progression.',
    'The signal may be relevant, TP53 caused tumor progression.',
    'The signal may be relevant but TP53 drives tumor progression.',
    '该信号可能相关，TP53驱动肿瘤进展。', 'TP53 directly regulates tumor progression.'])
def test_adjacent_hedge_does_not_license_causal_assertion(tmp_path, statement):
    content = draft()
    content['interpretations'][0]['statement'] = statement
    with pytest.raises(ValueError, match='causal'):
        phases.validate_draft(session(tmp_path), content)


@pytest.mark.parametrize('statement', ['TP53 might mediate a stress response.',
    'TP53 could regulate signaling.', 'The data do not prove causality.', 'TP53可能介导应激反应。'])
def test_normal_qualified_biological_reasoning_is_allowed(tmp_path, statement):
    content = draft()
    content['interpretations'][0]['statement'] = statement
    phases.validate_draft(session(tmp_path), content)


@pytest.mark.parametrize('metadata', [[{'field': 'isRetracted', 'value': 'N'}],
    [{'field': 'isRetracted', 'value': False}], [{'relation': 'ErratumIn', 'pmid': '99999'}],
    [{'field': 'commentCorrectionList', 'value': {'commentCorrection': [{'type': 'CommentIn'}]}}],
    [{'relation': 'RetractionOf', 'pmid': '99999'}]])
def test_nonretracted_flags_and_corrections_do_not_trigger_false_retraction(tmp_path, metadata):
    value = session(tmp_path)
    value.literature_result['records'] = [paper(metadata)]
    phases.validate_draft(value, draft(with_literature=True))


@pytest.mark.parametrize('metadata', [[{'field': 'isRetracted', 'value': 'Y'}],
    [{'field': 'isRetracted', 'value': True}], [{'relation': 'RetractionIn', 'pmid': '99999'}]])
def test_true_retraction_cannot_establish_support_but_can_be_documented(tmp_path, metadata):
    value = session(tmp_path)
    value.literature_result['records'] = [paper(metadata)]
    content = draft(with_literature=True)
    with pytest.raises(ValueError, match='Retracted'):
        phases.validate_draft(value, content)
    content['literature_claims'][0]['relation'] = 'unclear'
    phases.validate_draft(value, content)


def test_retracted_publication_type_is_also_respected(tmp_path):
    value = session(tmp_path)
    value.literature_result['records'][0]['publication_types'].append('Retracted Publication')
    with pytest.raises(ValueError, match='Retracted'):
        phases.validate_draft(value, draft(with_literature=True))


def test_direct_evidence_requires_known_and_matched_context(tmp_path):
    value = session(tmp_path)
    content = draft(with_literature=True)
    link = content['literature_claims'][0]
    link['directness'] = 'direct'
    with pytest.raises(ValueError, match='Direct evidence'):
        phases.validate_draft(value, content)
    link['context_match'] = {k: 'matched' for k in link['context_match']}
    phases.validate_draft(value, content)
    value.context.species = 'unknown'
    with pytest.raises(ValueError, match='Unknown study context'):
        phases.validate_draft(value, content)


def test_literature_report_cannot_exist_without_quote_and_local_observation_stays_separate(tmp_path):
    value = draft()
    value['interpretations'] = [interpretation(kind='literature_report', evidence_ids=[])]
    with pytest.raises(ValueError, match='verified passage'):
        phases.validate_draft(session(tmp_path), value)
    value['literature_claims'] = [relation()]
    phases.validate_draft(session(tmp_path), value)
    value['interpretations'][0]['kind'] = 'statistical_fact'
    with pytest.raises(jsonschema.ValidationError):
        phases.validate_draft(session(tmp_path), value)


def test_schema_rejects_extra_stats_and_whitespace_statement(tmp_path):
    value = draft()
    value['interpretations'][0]['p_value'] = 0.01
    with pytest.raises(jsonschema.ValidationError):
        phases.validate_draft(session(tmp_path), value)
    value = draft()
    value['interpretations'][0]['statement'] = ' '
    with pytest.raises(jsonschema.ValidationError):
        phases.validate_draft(session(tmp_path), value)


@pytest.mark.parametrize('status', ['no_results', 'failed', 'offline', 'partial'])
def test_search_failure_or_no_results_cannot_create_novelty(tmp_path, status):
    value = session(tmp_path)
    value.literature_result['status'] = status
    content = draft(with_literature=True)
    content['novelty'] = [{'claim_id': 'claim_1e256', 'assessment': 'potentially_novel',
                          'source_ids': [SID], 'rationale': 'A context-specific gap may remain.'}]
    with pytest.raises(ValueError, match=status):
        phases.validate_draft(value, content)
    content['novelty'][0]['assessment'] = 'undetermined'
    phases.validate_draft(value, content)


def test_known_publication_can_be_reported_despite_another_provider_failure(tmp_path):
    value = session(tmp_path)
    value.literature_result['status'] = 'partial'
    content = draft(with_literature=True)
    content['novelty'] = [{'claim_id': 'claim_1e256', 'assessment': 'reported',
                          'source_ids': [SID], 'rationale': 'A retrieved study reported this association.'}]
    phases.validate_draft(value, content)


def test_novelty_citations_and_absolute_priority_are_checked(tmp_path):
    value = draft()
    value['novelty'] = [{'claim_id': 'claim_1e256', 'assessment': 'reported',
                         'source_ids': [SID], 'rationale': 'A study reported this.'}]
    with pytest.raises(ValueError, match='verified claim/source'):
        phases.validate_draft(session(tmp_path), value)
    value['literature_claims'] = [relation()]
    value['novelty'][0]['rationale'] = 'This is the first-ever finding.'
    with pytest.raises(ValueError, match='absolute priority'):
        phases.validate_draft(session(tmp_path), value)
    value['novelty'][0]['rationale'] = 'We cannot claim first-ever priority.'
    phases.validate_draft(session(tmp_path), value)


def test_experiment_sources_need_verified_hypothesis_source_pair(tmp_path):
    value = draft(with_hypothesis=True)
    value['hypotheses'][0]['validation_experiments'][0]['source_ids'] = [SID]
    with pytest.raises(ValueError, match='Experiment literature'):
        phases.validate_draft(session(tmp_path), value)
    value['literature_claims'] = [relation(claim_id='hypothesis_1')]
    phases.validate_draft(session(tmp_path), value)


@pytest.mark.parametrize('pair', [('unknown_claim', SID), ('claim_1e256', 'PMID:unknown')])
def test_critic_rejects_unknown_or_missing_pairs(tmp_path, pair):
    value = session(tmp_path, {'critic': review([pair])})
    with pytest.raises(ValueError, match='every literature claim/source pair'):
        value.critic(draft(with_literature=True), [])


def test_critic_reviews_each_pair_once_and_cannot_accept_unresolved_issues(tmp_path):
    candidate = review([('claim_1e256', SID), ('claim_1e256', SID)])
    value = session(tmp_path, {'critic': candidate})
    with pytest.raises(ValueError, match='exactly once'):
        value.critic(draft(with_literature=True), [])
    candidate = review([('claim_1e256', SID)])
    candidate['literature_review'][0]['decision'] = 'insufficient'
    value.client.replies['critic'] = candidate
    with pytest.raises(ValueError, match='unresolved'):
        value.critic(draft(with_literature=True), [])


def test_analyst_failure_does_not_erase_literature_and_remains_unresolved(tmp_path):
    value = session(tmp_path, {'analyst': LLMValidationError('invalid finding'),
                               'librarian': {'queries': ['TP53 tumor stress']}})
    result = value.analysts_and_librarian()
    assert result['interpretations'] == [] and result['limitations']
    assert value.literature_service.calls and value.literature_result['records']
    assert value.errors == [{'step': 'analyst_research', 'message': 'LLMValidationError', 'resolved': False}]


def test_librarian_failure_preserves_analyst_and_is_failed_not_no_results(tmp_path):
    value = session(tmp_path, {'analyst': {'interpretations': [interpretation()], 'limitations': []},
                               'librarian': LLMValidationError('invalid query')})
    result = value.analysts_and_librarian()
    assert result['interpretations'] and value.literature_result['status'] == 'failed'
    assert not value.literature_service.calls and not value.errors[0]['resolved']


@pytest.mark.parametrize('query_count,limit', [(9, 8), (3, 2)])
def test_librarian_query_limit_is_validated_before_retrieval(tmp_path, query_count, limit):
    value = session(tmp_path, {'analyst': {'interpretations': [interpretation()], 'limitations': []},
                               'librarian': {'queries': ['TP53 stress'] * query_count}})
    value.literature_service.settings = {'max_queries': limit}
    result = value.analysts_and_librarian()
    assert result['interpretations'] and value.literature_result['status'] == 'failed'
    assert value.literature_service.calls == []
    assert value.errors[0]['step'] == 'librarian'
    supplied = json.loads(value.client.calls[-1]['user'])
    assert supplied['retrieval_limits']['max_queries'] == limit


@pytest.mark.parametrize('error', [PrivacyViolation('private data'), LLMBudgetExceeded('budget exhausted')])
def test_privacy_and_budget_failures_propagate_without_more_calls(tmp_path, error):
    value = session(tmp_path, {'analyst': error, 'librarian': {'queries': ['TP53']}})
    with pytest.raises(type(error)):
        value.analysts_and_librarian()
    assert len(value.client.calls) == 1 and not value.literature_service.calls


def test_context_budget_stratifies_omics_layers_and_unselected_unavailable_rows(tmp_path):
    context = session(tmp_path).context
    context.evidence = [observation('rna_' + str(i), padj=0.00001) for i in range(200)]
    for omics, layer in [('transcriptomics', 'de'), ('proteomics', 'de'), ('metabolomics', 'de'),
                         ('transcriptomics', 'pathway_enrichment')]:
        for category, selected, padj in [('selected', True, 0.01), ('negative', False, 0.9), ('na', False, None)]:
            context.evidence.append(observation(omics + '_' + layer + '_' + category,
                                                omics, layer, selected, padj))
    records = phases.qualitative_records(context, limit=12)
    assert len(records) == 12
    for omics, layer in [('transcriptomics', 'de'), ('proteomics', 'de'), ('metabolomics', 'de'),
                         ('transcriptomics', 'pathway_enrichment')]:
        chosen = [r for r in records if r['source']['omics'] == omics and r['source']['evidence_layer'] == layer]
        assert len(chosen) == 3
        assert any(r['observation']['selected'] is False for r in chosen)
        assert any(r['observation']['status'] == 'not_estimable' for r in chosen)
    coverage = phases.evidence_coverage(context, records)
    assert not coverage['complete'] and sum(r['shown'] for r in coverage['strata']) == 12
    assert 'effect' not in records[0]['observation'] and 'adjusted_p' not in records[0]['observation']


def test_registry_summary_counts_every_dimension_and_result_status(tmp_path):
    context = session(tmp_path).context
    context.evidence = [
        observation('ev_selected', selected=True, padj=0.001),
        observation('ev_unselected', selected=False, padj=0.5),
        observation('ev_unavailable', selected=False, padj=None, status='not_estimable'),
        observation('ev_protein', omics='proteomics', entity_id='CD3E', entity_type='protein'),
        observation('ev_pathway', layer='pathway_enrichment', entity_id='HALLMARK_TNFA',
                    entity_type='pathway'),
    ]
    summary = phases.registry_summary(context)
    assert summary['total'] == 5
    assert (summary['selected'], summary['unselected'], summary['unavailable']) == (3, 1, 1)
    for dimension in ('omics', 'evidence_layer', 'evidence_context', 'cohort', 'entity_type'):
        assert sum(summary[dimension].values()) == summary['total']
    assert summary['omics'] == {'transcriptomics': 4, 'proteomics': 1}
    assert summary['evidence_layer'] == {'de': 4, 'pathway_enrichment': 1}
    assert summary['evidence_context'] == {'derived_estimate': 1, 'measured_evidence': 4}
    assert summary['entity_type'] == {'gene': 3, 'pathway': 1, 'protein': 1}


def hidden_registry(context):
    """300 rows whose target entities and counterevidence sort past the 120 display cap."""
    context.question = 'Explain TP53 signaling in T cell states.'
    rows = [observation('fill_%03d' % index, padj=0.001, entity_id='FILLER_%03d' % index)
            for index in range(295)]
    rows.extend([
        observation('target_gene', padj=0.9, entity_id='TP53'),
        observation('target_path', padj=0.91, entity_id='HALLMARK_TNFA_SIGNALING',
                    entity_type='pathway', symbol='TNFA_SIGNALING'),
        observation('target_cell', padj=0.92, entity_id='CD3E', entity_type='protein',
                    cell_type='T cell'),
        observation('counter_low', padj=0.93, entity_id='TP53', direction='lower_in_numerator'),
        observation('counter_same', padj=0.94, entity_id='TP53'),
    ])
    context.evidence = rows
    return {'target_gene', 'target_path', 'target_cell', 'counter_low', 'counter_same'}


def test_search_and_phase_evidence_reach_rows_hidden_by_the_bounded_display(tmp_path):
    context = session(tmp_path).context
    target_ids = hidden_registry(context)
    display = phases.qualitative_records(context)
    assert len(display) == 120
    assert target_ids.isdisjoint({r['evidence_id'] for r in display})

    phase = phases.phase_evidence(context)
    assert len(phase['observations']) == 120
    assert phase['registry_summary']['total'] == 300
    assert target_ids <= {r['evidence_id'] for r in phase['targeted_observations']}
    assert phase['registry_summary']['entity_type'] == {'gene': 298, 'pathway': 1, 'protein': 1}
    assert phase['coverage']['targeted_complete'] is True
    assert phase['coverage']['display_truncated'] is True
    assert phase['coverage']['complete'] is False
    row = phase['targeted_observations'][0]
    assert {'effect', 'p_value', 'adjusted_p', 'n', 'status', 'direction', 'selected',
            'effect_scale', 'missing_fields'} <= set(row['observation'])

    genes = phases.search_evidence(context, ['TP53'], limit=600)
    assert genes['matched'] == 3 and genes['returned'] == 3 and genes['truncated'] is False
    assert {r['evidence_id'] for r in genes['observations']} == {'target_gene', 'counter_low',
                                                                 'counter_same'}
    assert [r['evidence_id'] for r in phases.search_evidence(context, ['HALLMARK'])['observations']] \
        == ['target_path']
    assert [r['evidence_id'] for r in phases.search_evidence(context, ['T cell'])['observations']] \
        == ['target_cell']
    assert phases.search_evidence(context, ['lower_in_numerator'])['matched'] == 1
    assert phases.search_evidence(context, ['measured_evidence'])['matched'] == 300
    assert phases.search_evidence(context, [])['matched'] == 0

    bounded = phases.search_evidence(context, ['TP53'], limit=2)
    assert (bounded['matched'], bounded['returned'], bounded['truncated']) == (3, 2, True)
    exact = phases.search_evidence(context, ['TP53'], limit=3)
    assert exact['matched'] == 3 and exact['truncated'] is False
    capped = phases.phase_evidence(context, ['TP53'], targeted_limit=2)
    assert capped['targeted_truncated'] is True
    assert capped['coverage']['targeted_complete'] is False
    narrowed = phases.phase_evidence(context, ['TP53'], observation_limit=7, targeted_limit=3)
    assert len(narrowed['observations']) == 7 and narrowed['targeted_truncated'] is False


def test_phases_target_full_rows_from_analyst_and_cluster_entities(tmp_path):
    value = session(tmp_path, {'analyst': {'interpretations': [interpretation(
                  evidence_ids=['target_gene'])], 'limitations': []},
              'librarian': {'queries': ['TP53 tumor stress']}})
    hidden_registry(value.context)
    analysts = value.analysts_and_librarian()
    analyst_payload = json.loads(value.client.calls[0]['user'])
    assert 'TP53' in analyst_payload['query_terms']
    assert 'target_gene' in {r['evidence_id'] for r in analyst_payload['targeted_observations']}

    value.client.replies['integrator'] = {'clusters': [{'id': 'cluster_1e256', 'topic': 'TP53',
        'supporting_evidence_ids': ['target_gene'], 'counterevidence_ids': ['counter_low'],
        'interpretation': 'The observations may reflect stress signaling.', 'missing_links': [],
        'direction_note': 'Different molecules have different directions.'}]}
    value.integrate(analysts)
    integrator_payload = json.loads(value.client.calls[-1]['user'])
    integrator_targeted = {r['evidence_id'] for r in integrator_payload['targeted_observations']}
    assert {'target_gene', 'counter_low', 'counter_same'} <= integrator_targeted

    reply = draft()
    reply['interpretations'][0]['evidence_ids'] = ['target_gene']
    value.client.replies['reasoner'] = reply
    value.reason({'clusters': [{'id': 'cluster_1e256', 'supporting_evidence_ids': ['target_gene'],
                                'counterevidence_ids': []}]})
    reasoner_payload = json.loads(value.client.calls[-1]['user'])
    reasoner_targeted = {r['evidence_id'] for r in reasoner_payload['targeted_observations']}
    assert {'target_gene', 'counter_low', 'counter_same'} <= reasoner_targeted
    assert reasoner_payload['registry_summary']['total'] == 300


def test_critic_requires_complete_counterevidence_and_negative_review(tmp_path):
    pairs = [('claim_1e256', SID)]
    value = session(tmp_path, {'critic': review(pairs)})
    value.context.evidence = [
        observation(EID, selected=True, padj=0.01),
        observation('ev_not_selected', selected=False, padj=0.5),
        observation('ev_not_estimable', selected=False, padj=None, status='not_estimable'),
        observation('ev_opposite', selected=True, padj=0.02, direction='lower_in_numerator'),
        observation('ev_other_gene', entity_id='GENE_B', selected=False, padj=0.9),
    ]
    candidates = ['ev_not_selected', 'ev_not_estimable', 'ev_opposite']
    negatives = ['ev_not_selected', 'ev_not_estimable']
    synthesis = draft(with_literature=True)

    with pytest.raises(ValueError, match='counterevidence'):
        value.critic(synthesis, [])
    value.client.replies['critic'] = review(pairs, reviews=[
        evidence_review(counter=candidates[:1], negative=negatives)])
    with pytest.raises(ValueError, match='counterevidence'):
        value.critic(synthesis, [])
    value.client.replies['critic'] = review(pairs, reviews=[
        evidence_review(counter=candidates, negative=[])])
    with pytest.raises(ValueError, match='negative'):
        value.critic(synthesis, [])
    value.client.replies['critic'] = review(pairs, reviews=[
        evidence_review(reviewed=[], counter=candidates, negative=negatives)])
    with pytest.raises(ValueError, match='exactly the claim evidence IDs'):
        value.critic(synthesis, [])
    for assessment in ('concern', 'insufficient'):
        value.client.replies['critic'] = review(pairs, reviews=[
            evidence_review(counter=candidates, negative=negatives, assessment=assessment)])
        with pytest.raises(ValueError, match='consistent'):
            value.critic(synthesis, [])
    value.client.replies['critic'] = review(pairs, reviews=[
        evidence_review(counter=candidates, negative=negatives)])
    result = value.critic(synthesis, [])
    assert result['decision'] == 'accept'
    payload = json.loads(value.client.calls[-1]['user'])
    assert {r['evidence_id'] for r in payload['counterevidence_candidates']['claim_1e256']} \
        == set(candidates)
    assert payload['negative_result_ids']['claim_1e256'] == negatives

    value.client.replies['critic'] = review([('claim_1e256', SID)], reviews=[
        evidence_review(counter=candidates, negative=negatives)])
    with pytest.raises(ValueError, match='every claim exactly once'):
        value.critic(draft(with_hypothesis=True, with_literature=True), [])


def test_excessive_counterevidence_blocks_without_a_model_call(tmp_path):
    value = session(tmp_path, {'critic': review()})
    value.context.evidence = [observation(EID)]
    value.context.evidence.extend(observation('ev_%04d' % index, padj=0.9)
                                  for index in range(601))
    with pytest.raises(ValueError, match='exceed the review budget'):
        value.critic(draft(), [])
    assert value.client.calls == []


@pytest.mark.parametrize('statement', ['TP53 was significantly higher in tumor cells.',
                                       'TP53表达显著升高。',
                                       'TP53 was FDR-significant in tumor cells.'])
def test_significance_language_requires_verified_selected_evidence(tmp_path, statement):
    value = session(tmp_path)
    content = draft()
    content['interpretations'][0]['statement'] = statement

    value.context.evidence = [observation(selected=None, selection=frozen_selection(False, None))]
    with pytest.raises(ValueError, match='verified selected evidence'):
        phases.validate_draft(value, content)
    value.context.evidence = [observation(selected=False, padj=0.5,
                                          selection=frozen_selection(True, False))]
    with pytest.raises(ValueError, match='verified selected evidence'):
        phases.validate_draft(value, content)
    value.context.evidence = [observation(selection=frozen_selection(True, True))]
    phases.validate_draft(value, content)
    # A direct legacy fixture without the frozen selection contract stays tolerated.
    value.context.evidence = [observation()]
    phases.validate_draft(value, content)
    # Unverified selection metadata does not block non-significance prose.
    content['interpretations'][0]['statement'] = 'TP53 abundance may reflect altered stress signaling.'
    value.context.evidence = [observation(selected=None, selection=frozen_selection(False, None))]
    phases.validate_draft(value, content)



@pytest.mark.parametrize('statement', ['TP53 drives flux.', 'TP53 increases metabolic flux.',
                                       'These cells are the cell of origin.', 'TP53通量增加。'])
def test_flux_activity_and_cell_of_origin_overreach_is_rejected(tmp_path, statement):
    content = draft()
    content['interpretations'][0]['statement'] = statement
    with pytest.raises(ValueError, match='overreach'):
        phases.validate_draft(session(tmp_path), content)
    hypothesis_content = draft(with_hypothesis=True)
    hypothesis_content['hypotheses'][0]['statement'] = statement
    with pytest.raises(ValueError, match='overreach'):
        phases.validate_draft(session(tmp_path), hypothesis_content)


@pytest.mark.parametrize('statement', ['TP53 may alter flux.', 'enzyme activity was not measured.',
                                       'a candidate cell-of-origin hypothesis.'])
def test_qualified_flux_activity_and_origin_language_is_accepted(tmp_path, statement):
    content = draft()
    content['interpretations'][0]['statement'] = statement
    phases.validate_draft(session(tmp_path), content)


def test_counterevidence_matches_symbol_across_entity_namespaces(tmp_path):
    context = session(tmp_path).context
    cited = observation('ev_' + 'a' * 20, entity_id='ENSG00000141510', symbol='TP53')
    other = observation('ev_' + 'b' * 20, entity_id='TP53', symbol='TP53', padj=0.9,
                        selected=False, status='available')
    context.evidence = [cited, other]
    basis = phases.counterevidence_candidates(
        context, [{'id': 'I1', 'evidence_ids': [cited['evidence_id']]}])
    assert basis['I1']['counterevidence_ids'] == [other['evidence_id']]

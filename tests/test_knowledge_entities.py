"""Explicit species/entity identities and bounded, reviewable experimental know-how."""
from copy import deepcopy

import pytest

from orchestrator.knowledge import KnowledgeError, KnowledgeService, validate_payload
from test_scientific_wiki import approve, literature_payload


@pytest.mark.parametrize('identifier,kind', [
    ('TP53', 'gene'), ('gene:TP53', 'gene'), ('gene:human:TP53', 'gene'),
    ('uniprot:P04637', 'protein'), ('cl:CD8', 'cell_type'), ('gene:9606:TP53', 'made_up_type')])
def test_entity_identity_requires_explicit_namespace_species_and_type(identifier, kind):
    payload = literature_payload()
    payload['entities'][0].update(id=identifier, type=kind)
    with pytest.raises(KnowledgeError):
        validate_payload(payload)


def test_same_alias_does_not_merge_species_or_gene_and_protein(tmp_path):
    service = KnowledgeService(tmp_path)
    entries = []
    for identifier, kind, species in [('ncbigene:9606:7157', 'gene', 'human'),
                                      ('ncbigene:10090:22059', 'gene', 'mouse'),
                                      ('uniprot:9606:P04637', 'protein', 'human')]:
        payload = literature_payload(identifier)
        payload['context']['species'] = species
        payload['entities'][0].update(id=identifier, type=kind, label='TP53', aliases=['P53'])
        entries.append(approve(service, payload))
    assert len({entry['entry_id'] for entry in entries}) == 3
    assert len(service.search('P53')) == 3
    assert len(service.search('P53', context={'species': 'mouse'})) == 1
    assert all(not item['backlinks'] for item in service.search('P53'))


def test_know_how_requires_applicability_and_failures_then_reuses_reviewed_version(tmp_path):
    service = KnowledgeService(tmp_path)
    payload = literature_payload('Know-How expression perturbation pilot')
    payload['kind'] = 'know_how'
    fields = {'applicability': 'Exploratory expression perturbation.',
              'prerequisites': 'A suitable model and orthogonal assay are required.',
              'method_basis': 'Method scope is limited to the attached source.',
              'common_failures': 'Unknown; record negative and failed pilots.',
              'limitations': 'No fixed dose or sample size is established.'}
    payload['context'].update(fields)
    for key in fields:
        incomplete = deepcopy(payload)
        del incomplete['context'][key]
        with pytest.raises(KnowledgeError, match='Know-How'):
            service.propose(incomplete)
    proposed = service.propose(payload)
    assert service.snapshot('perturbation')['entries'] == []
    entry = service.review(proposed['id'], proposed['sha256'], 'approve')
    frozen = service.snapshot('perturbation')
    assert frozen['entries'][0]['context']['common_failures'] == fields['common_failures']
    service.retract(entry['entry_id'], entry['sha256'])
    assert service.snapshot('perturbation')['entries'] == []
    assert frozen['entries'][0]['revision'] == 1

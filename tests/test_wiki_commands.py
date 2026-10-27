"""The Wiki command parser never turns command text into human approval."""
from copy import deepcopy
import hashlib
import json

import pytest

from multiomics_tui.wiki import handle_wiki, execute_wiki
from orchestrator.knowledge_context import knowledge_service


def payload(title='Reviewed test knowledge'):
    text = 'An official abstract reported GENE1 in a test disease model.'
    return {'title': title, 'kind': 'literature_report', 'statement': 'The publication reports GENE1.',
            'context': {'species': 'human'}, 'sources': [{
                'source_id': 'pubmed:123', 'source_type': 'publication', 'primary': True,
                'locator': 'https://pubmed.ncbi.nlm.nih.gov/123/', 'text': text,
                'quote': 'reported GENE1', 'sha256': hashlib.sha256(text.encode()).hexdigest(),
                'metadata': {'content_scope': 'abstract'}}],
            'entities': [{'id': 'gene:9606:GENE1', 'type': 'gene', 'label': 'GENE1'}], 'relations': []}


@pytest.fixture
def settings(tmp_path):
    return {'shared_root': str(tmp_path / 'shared')}


def resource(root, value, name='entries.json'):
    path = root / 'knowledge/resources' / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    return str(path.relative_to(root))


def accepted(root, settings, value=None):
    service = knowledge_service(root, settings, read_only=False)
    proposal = service.propose(value or payload())
    return service.review(proposal['id'], proposal['sha256'], 'approve')


def test_read_commands_leave_empty_workspace_untouched(tmp_path, settings):
    root = tmp_path / 'project'
    for command in ('', 'help', '/wiki', 'search GENE1', 'pending'):
        result = handle_wiki(root, command, settings)
        assert 'message' in result and 'confirmation' not in result
        assert not result.get('error')
    assert not root.exists()
    assert not (tmp_path / 'shared').exists()


def test_import_and_review_are_separate_content_bound_confirmations(tmp_path, settings):
    path = resource(tmp_path, [payload(), payload('Another report')])
    preview = handle_wiki(tmp_path, f'import {path}', settings)
    assert not preview.get('error')
    assert '2 条候选' in preview['message']
    assert not (tmp_path / 'knowledge/wiki').exists()
    result = execute_wiki(tmp_path, preview['confirmation'], settings)
    assert not result.get('error')
    service = knowledge_service(tmp_path, settings)
    assert len(service.pending()) == 2
    assert service.search('GENE1') == []
    review = handle_wiki(tmp_path, 'review all', settings)
    assert review['confirmation']['arguments']['decision'] == 'approve'
    assert '原文定位:' in review['message'] and '不会自动发布' in review['message']
    assert service.search('GENE1') == []
    assert not execute_wiki(tmp_path, review['confirmation'], settings).get('error')
    assert len(service.search('GENE1')) == 2
    assert not (tmp_path / 'shared').exists()


def test_single_review_reject_and_show_history(tmp_path, settings):
    service = knowledge_service(tmp_path, settings, read_only=False)
    proposal = service.propose(payload())
    preview = handle_wiki(tmp_path, f'review {proposal["id"]} reject', settings)
    assert not execute_wiki(tmp_path, preview['confirmation'], settings).get('error')
    assert not service.pending() and not service.search('GENE1')
    entry = accepted(tmp_path, settings, payload('Different report'))
    show = handle_wiki(tmp_path, f'show {entry["entry_id"]} 1', settings)
    assert '版本: 1' in show['message'] and 'pubmed:123' in show['message']


@pytest.mark.parametrize('command', [
    'review all --yes', 'review all -y', 'publish kb_x --yes', 'import knowledge/resources/a.json --yes',
    'review all yes', 'show kb_x zero', 'review', 'publish', 'bogus', 'search "unfinished',
])
def test_invalid_commands_and_yes_flags_do_not_write(tmp_path, settings, command):
    result = handle_wiki(tmp_path, command, settings)
    assert result.get('error')
    assert 'confirmation' not in result
    assert not (tmp_path / 'knowledge/wiki').exists()


def test_import_file_must_match_preview_bytes(tmp_path, settings):
    path = resource(tmp_path, payload())
    preview = handle_wiki(tmp_path, f'import {path}', settings)
    with (tmp_path / path).open('a') as handle:
        handle.write('\n')
    result = execute_wiki(tmp_path, preview['confirmation'], settings)
    assert result.get('error') and '预览后发生变化' in result['message']
    assert not (tmp_path / 'knowledge/wiki').exists()


def test_confirmation_cannot_be_tampered_or_moved_between_workspaces(tmp_path, settings):
    path = resource(tmp_path, payload())
    confirmation = handle_wiki(tmp_path, f'import {path}', settings)['confirmation']
    tampered = deepcopy(confirmation)
    tampered['arguments']['payloads'][0]['statement'] = 'An invented mechanism.'
    assert execute_wiki(tmp_path, tampered, settings).get('error')
    assert execute_wiki(tmp_path / 'other', confirmation, settings).get('error')
    assert execute_wiki(tmp_path, confirmation, {'shared_enabled': False}).get('error')
    assert not (tmp_path / 'knowledge/wiki').exists()


@pytest.mark.parametrize('path', ['../private.json', '/tmp/private.json',
                                   'data/raw/private.json', 'knowledge/resources/../../private.json',
                                   'knowledge/resources/file.txt', 'knowledge\\resources\\file.json'])
def test_import_path_is_confined_to_resources(tmp_path, settings, path):
    assert handle_wiki(tmp_path, f'import "{path}"', settings).get('error')
    assert not (tmp_path / 'knowledge/wiki').exists()


def test_import_symlink_and_parent_symlink_rejected(tmp_path, settings):
    original = tmp_path / 'private.json'
    original.write_text(json.dumps(payload()))
    directory = tmp_path / 'knowledge/resources'
    directory.mkdir(parents=True)
    (directory / 'linked.json').symlink_to(original)
    assert handle_wiki(tmp_path, 'import knowledge/resources/linked.json', settings).get('error')
    (directory / 'subdir').symlink_to(tmp_path, target_is_directory=True)
    assert handle_wiki(tmp_path, 'import knowledge/resources/subdir/private.json', settings).get('error')


def test_symlink_swap_after_preview_is_rejected(tmp_path, settings):
    path = resource(tmp_path, payload())
    preview = handle_wiki(tmp_path, f'import {path}', settings)
    outside = tmp_path / 'private.json'
    outside.write_bytes((tmp_path / path).read_bytes())
    (tmp_path / path).unlink()
    (tmp_path / path).symlink_to(outside)
    assert execute_wiki(tmp_path, preview['confirmation'], settings).get('error')
    assert not (tmp_path / 'knowledge/wiki').exists()


def test_review_all_freezes_exact_candidates_and_is_atomic_on_conflict(tmp_path, settings):
    service = knowledge_service(tmp_path, settings, read_only=False)
    first = service.propose(payload('First'))
    second = service.propose(payload('Second'))
    preview = handle_wiki(tmp_path, 'review all', settings)
    third = service.propose(payload('Arrived after preview'))
    # Another reviewer acts after the UI has shown the old pending set.
    service.review(second['id'], second['sha256'], 'reject')
    result = execute_wiki(tmp_path, preview['confirmation'], settings)
    assert result.get('error')
    assert {p['id'] for p in service.pending()} == {first['id'], third['id']}
    assert service.search('GENE1') == []
    next_preview = handle_wiki(tmp_path, 'review all', settings)
    fourth = service.propose(payload('Still later'))
    assert not execute_wiki(tmp_path, next_preview['confirmation'], settings).get('error')
    assert [p['id'] for p in service.pending()] == [fourth['id']]


def test_import_batch_rolls_back_on_second_entry_conflict(tmp_path, settings):
    current = accepted(tmp_path, settings)
    update = payload()
    update.update(entry_id=current['entry_id'], base_sha256=current['sha256'], statement='Changed text.')
    path = resource(tmp_path, [payload('New candidate'), update])
    preview = handle_wiki(tmp_path, f'import {path}', settings)
    concurrent = deepcopy(update)
    concurrent['statement'] = 'Another reviewer updated this.'
    accepted(tmp_path, settings, concurrent)
    result = execute_wiki(tmp_path, preview['confirmation'], settings)
    assert result.get('error')
    assert knowledge_service(tmp_path, settings).pending() == []


def test_publish_and_retract_require_independent_previews(tmp_path, settings):
    value = payload()
    value['shareable'] = True
    entry = accepted(tmp_path, settings, value)
    publish = handle_wiki(tmp_path, f'publish {entry["entry_id"]}', settings)
    assert '共享库:' in publish['message'] and not (tmp_path / 'shared').exists()
    assert not execute_wiki(tmp_path, publish['confirmation'], settings).get('error')
    reader = knowledge_service(tmp_path / 'other', settings)
    assert reader.search('GENE1')
    retraction = handle_wiki(tmp_path, f'retract {entry["entry_id"]}', settings)
    assert reader.search('GENE1')
    assert not execute_wiki(tmp_path, retraction['confirmation'], settings).get('error')
    assert reader.search('GENE1') == []
    assert 'retracted' in handle_wiki(tmp_path, f'show {entry["entry_id"]}', settings)['message']


def test_publish_preview_rejects_private_or_unreviewed_content(tmp_path, settings):
    entry = accepted(tmp_path, settings)
    assert handle_wiki(tmp_path, f'publish {entry["entry_id"]}', settings).get('error')
    value = payload('Private data')
    value.update(shareable=True, context={'patient_id': 'P1'})
    private = accepted(tmp_path, settings, value)
    assert handle_wiki(tmp_path, f'publish {private["entry_id"]}', settings).get('error')
    assert handle_wiki(tmp_path, f'retract shared:{entry["entry_id"]}', settings).get('error')
    assert not (tmp_path / 'shared').exists()


def test_changed_entry_cannot_be_published_under_old_confirmation(tmp_path, settings):
    value = payload()
    value['shareable'] = True
    entry = accepted(tmp_path, settings, value)
    preview = handle_wiki(tmp_path, f'publish {entry["entry_id"]}', settings)
    value.update(entry_id=entry['entry_id'], base_sha256=entry['sha256'], statement='A corrected statement.')
    accepted(tmp_path, settings, value)
    assert execute_wiki(tmp_path, preview['confirmation'], settings).get('error')
    assert not (tmp_path / 'shared').exists()


def test_snapshot_export_is_confirmed_and_refuses_stale_or_existing_destination(tmp_path, settings):
    accepted(tmp_path, settings)
    command = 'export knowledge/exports/snapshot-1 GENE1'
    preview = handle_wiki(tmp_path, command, settings)
    directory = tmp_path / 'knowledge/exports/snapshot-1'
    assert not directory.exists()
    assert not execute_wiki(tmp_path, preview['confirmation'], settings).get('error')
    assert (directory / 'snapshot.json').is_file() and (directory / 'snapshot.md').is_file()
    assert handle_wiki(tmp_path, command, settings).get('error')
    preview = handle_wiki(tmp_path, 'export knowledge/exports/snapshot-2 GENE1', settings)
    accepted(tmp_path, settings, payload('Additional evidence'))
    assert execute_wiki(tmp_path, preview['confirmation'], settings).get('error')
    assert not (tmp_path / 'knowledge/exports/snapshot-2').exists()


def test_invalid_payload_rejected_before_import_confirmation(tmp_path, settings):
    value = payload()
    value['sources'][0]['quote'] = 'Invented quote'
    path = resource(tmp_path, value)
    result = handle_wiki(tmp_path, f'import {path}', settings)
    assert result.get('error') and 'confirmation' not in result
    assert not (tmp_path / 'knowledge/wiki').exists()

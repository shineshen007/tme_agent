"""Scientific Wiki commands with content-bound, explicit write confirmation.

Command preparation is read-only. The controller calls ``execute_wiki`` only
after its normal human confirmation flow; command text never grants approval.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import shlex
import sqlite3

from orchestrator.knowledge import (
    KnowledgeError, KnowledgeConflictError, canonical_json, content_sha256, validate_payload,
)
from orchestrator.knowledge_context import knowledge_service


MAX_IMPORT_BYTES = 2 * 1024 * 1024
MAX_IMPORT_ENTRIES = 50
HELP = (
    'Scientific Wiki\n'
    '/wiki search <query>: search reviewed knowledge\n'
    '/wiki show <entry-id> [revision]: view knowledge and sources\n'
    '/wiki pending: list pending candidates\n'
    '/wiki import knowledge/resources/<file>.json: preview candidate import\n'
    '/wiki review <candidate-id|all> [approve|reject]: preview human review\n'
    '/wiki retract <entry-id>: preview retraction\n'
    '/wiki publish <entry-id>: preview publication to the shared library\n'
    '/wiki export knowledge/exports/<new-directory> [query]: preview JSON/Markdown export\n'
    'Writes require confirmation; candidate import, knowledge approval and shared publication are separate actions.'
)


def _root(root):
    return Path(root).expanduser().resolve()


def _safe_path(root, raw, category, *, existing):
    """Confine command paths to one declared area and reject every symlink."""
    if not isinstance(raw, str) or not raw.strip() or '\\' in raw:
        raise KnowledgeError('Use a project-relative path')
    relative = PurePosixPath(raw)
    expected = ('knowledge', category)
    if relative.is_absolute() or '..' in relative.parts or relative.parts[:2] != expected or len(relative.parts) < 3:
        raise KnowledgeError(f'Path must be under knowledge/{category}/; absolute paths and .. are refused')
    root = _root(root)
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise KnowledgeError('Wiki import/export paths cannot use symbolic links')
    if not current.resolve().is_relative_to(root / 'knowledge' / category):
        raise KnowledgeError('Path leaves the allowed project directory')
    if existing:
        if not current.is_file():
            raise KnowledgeError('The specified Wiki resource does not exist')
        if current.suffix.lower() != '.json':
            raise KnowledgeError('Wiki imports accept JSON files only')
    elif current.exists():
        raise KnowledgeError('Export directory exists; choose a new directory to preserve the previous snapshot')
    return current


def _read_import(root, raw):
    path = _safe_path(root, raw, 'resources', existing=True)
    if path.stat().st_size > MAX_IMPORT_BYTES:
        raise KnowledgeError('A Wiki import file cannot exceed 2 MiB')
    content = path.read_bytes()
    if len(content) > MAX_IMPORT_BYTES:
        raise KnowledgeError('Wiki resource exceeded the size limit while reading')
    parsed = json.loads(content.decode('utf-8'))
    if isinstance(parsed, dict) and set(parsed) == {'entries'}:
        parsed = parsed['entries']
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list) or not 1 <= len(parsed) <= MAX_IMPORT_ENTRIES:
        raise KnowledgeError(f'Each import must contain 1–{MAX_IMPORT_ENTRIES} knowledge candidates')
    values = [validate_payload(value) for value in parsed]
    return hashlib.sha256(content).hexdigest(), values


def _display(value):
    # Documents remain data. Remove control characters from terminal previews.
    return ''.join(c for c in str(value) if c in '\n\t' or ord(c) >= 32 and ord(c) != 127)


def _entry_preview(item, *, previous=None):
    lines = [f'{_display(item["title"])} [{item["kind"]}]']
    if item.get('entry_id'):
        lines.append(f'ID: {item["entry_id"]}')
    if item.get('sha256'):
        lines.append(f'SHA256: {item["sha256"]}')
    if item.get('revision'):
        lines.append(f'Revision: {item["revision"]}; scope: {item.get("scope", "project")}; status: {item.get("status", "candidate")}')
    if previous is not None:
        lines.extend(['Previous statement: ' + _display(previous['statement']), 'New statement: ' + _display(item['statement'])])
    else:
        lines.append('Statement: ' + _display(item['statement']))
    lines.append('Context: ' + _display(canonical_json(item['context'])))
    for source in item['sources']:
        role = 'Primary source' if source['primary'] else 'Additional source'
        lines.append(f'- {role} {source["source_id"]} ({source["source_type"]}): {_display(source["locator"])}')
        if source.get('quote'):
            lines.append('  Source passage: ' + _display(source['quote']))
    if not item['sources']:
        lines.append('Sources: none; this entry cannot be treated as a verified fact.')
    if item['relations']:
        lines.append('Relations: ' + _display(canonical_json(item['relations'])))
    return '\n'.join(lines)


def _confirmation(root, service, operation, arguments):
    frozen = {'schema_version': '1.0', 'workspace': str(_root(root)),
              'library_root': str(service.root),
              'shared_root': str(service.shared_root) if service.shared_root else None,
              'operation': operation, 'arguments': arguments}
    frozen['payload_sha256'] = content_sha256(frozen)
    return frozen


def _prepared(root, service, operation, arguments, message):
    return {'message': message + '\n\nConfirm to execute these actions; cancel leaves the knowledge library unchanged.',
            'confirmation': _confirmation(root, service, operation, arguments)}


def _local_entry(service, entry_id):
    if entry_id.startswith('shared:'):
        raise KnowledgeError('Operate on local entries in their owning project; shared knowledge from another project cannot be edited directly')
    entry_id = entry_id.removeprefix('project:')
    entry = service.get('project:' + entry_id)
    if entry is None:
        raise KnowledgeError('No knowledge entry found for this project')
    return entry


def handle_wiki(root, text, settings=None):
    """Prepare a command; expected input errors become explicit UI messages."""
    try:
        return _handle_wiki(root, text, settings)
    except (KnowledgeError, OSError, UnicodeError, json.JSONDecodeError, sqlite3.DatabaseError) as exc:
        return {'message': f'Wiki action was not executed: {exc}', 'error': True}


def _handle_wiki(root, text, settings):
    try:
        args = shlex.split(text.strip())
    except ValueError as exc:
        raise KnowledgeError('Unclosed quote in command') from exc
    if args and args[0] == '/wiki':
        args.pop(0)
    if not args or args[0] in {'help', '?'}:
        return {'message': HELP}
    if '--yes' in args or '-y' in args:
        raise KnowledgeError('--yes/-y is not supported; inspect the preview and use the interface confirmation')
    command, params = args[0], args[1:]
    service = knowledge_service(root, settings, read_only=True)
    if command == 'search':
        entries = service.search(' '.join(params))
        if not entries:
            return {'message': 'No matching reviewed knowledge. Candidates are excluded from default search; use /wiki pending to inspect them.'}
        return {'message': '\n\n'.join(_entry_preview(item) for item in entries)}
    if command == 'show' and len(params) in {1, 2}:
        try:
            revision = int(params[1]) if len(params) == 2 else None
        except ValueError as exc:
            raise KnowledgeError('Revision must be a positive integer') from exc
        entry = service.get(params[0], revision)
        if entry is None:
            raise KnowledgeError('Entry or requested revision does not exist')
        message = _entry_preview(entry)
        if entry['backlinks']:
            message += '\nBacklinks: ' + _display(canonical_json(entry['backlinks']))
        return {'message': message}
    if command == 'pending' and not params:
        proposals = service.pending()
        if not proposals:
            return {'message': 'No pending knowledge candidates.'}
        return {'message': '\n\n'.join(
            f'{p["id"]}\nSHA256: {p["sha256"]}\n' + _entry_preview(p['payload']) for p in proposals)}
    if command == 'import' and len(params) == 1:
        file_sha, payloads = _read_import(root, params[0])
        previews = []
        for payload in payloads:
            old = service.get('project:' + payload['entry_id']) if payload.get('entry_id') else None
            if old and payload.get('base_sha256') != old['sha256']:
                raise KnowledgeConflictError('Update candidate lacks a matching base_sha256; check the current revision first')
            if not old and payload.get('base_sha256'):
                raise KnowledgeConflictError('Update target does not exist')
            previews.append(('Update candidate\n' if old else 'New candidate\n') + _entry_preview(payload, previous=old))
        message = (f'Import {len(payloads)} candidates from {params[0]}.\n'
                   f'File SHA256: {file_sha}\nSeparate human review is still required after import.\n\n' + '\n\n'.join(previews))
        return _prepared(root, service, 'import', {'path': params[0], 'file_sha256': file_sha,
                                                  'payloads': payloads}, message)
    if command == 'review' and len(params) in {1, 2}:
        decision = params[1] if len(params) == 2 else 'approve'
        if decision not in {'approve', 'reject'}:
            raise KnowledgeError('Review decision must be approve or reject')
        pending = service.pending()
        selected = pending if params[0] == 'all' else [p for p in pending if p['id'] == params[0]]
        if not selected:
            raise KnowledgeError('No matching pending candidate')
        if len(selected) > 100:
            raise KnowledgeError('Review at most 100 candidates at a time; split batches by candidate ID')
        if decision == 'approve' and len({p['entry_id'] for p in selected}) != len(selected):
            raise KnowledgeError('Multiple candidates exist for one entry; select by candidate ID and do not approve them together')
        action = 'approve into the project knowledge library' if decision == 'approve' else 'reject candidates'
        previews = []
        for item in selected:
            old = service.get('project:' + item['entry_id'])
            if decision == 'approve' and (old['sha256'] if old else None) != item['base_sha256']:
                raise KnowledgeConflictError('The candidate base revision changed; regenerate the candidate')
            previews.append(f'{item["id"]}\nSHA256: {item["sha256"]}\n' + _entry_preview(item['payload'], previous=old))
        return _prepared(root, service, 'review',
                         {'decision': decision, 'items': [{'id': p['id'], 'sha256': p['sha256']} for p in selected]},
                         f'Action: {action}; {len(selected)} candidates. Shared publication is separate.\n\n' + '\n\n'.join(previews))
    if command in {'retract', 'publish'} and len(params) == 1:
        entry = _local_entry(service, params[0])
        if entry['status'] != 'reviewed':
            raise KnowledgeError('Only currently reviewed, unretracted entries can be used')
        if command == 'publish':
            if service.shared_root is None:
                raise KnowledgeError('Shared Wiki is disabled')
            # A preflight only: the write method repeats all checks under lock.
            service._share_guard(entry)
            message = ('Publish the following reviewed entry to the explicitly configured shared library for other projects to retrieve.\n'
                       f'Shared library: {service.shared_root}\nCheck sources and de-identification.\n\n')
        else:
            message = 'Retract this entry and its shared versions published by this project; history is retained and pending updates are invalidated.\n\n'
        return _prepared(root, service, command,
                         {'entry_id': entry['entry_id'], 'expected_sha256': entry['sha256']},
                         message + _entry_preview(entry))
    if command == 'export' and params:
        _safe_path(root, params[0], 'exports', existing=False)
        query = ' '.join(params[1:])
        snapshot = service.snapshot(query)
        return _prepared(root, service, 'export',
                         {'path': params[0], 'query': query, 'snapshot_sha256': snapshot['sha256']},
                         f'Export {len(snapshot["entries"])} knowledge entries to {params[0]}.\n'
                         f'Snapshot SHA256: {snapshot["sha256"]}\nOutputs: snapshot.json, snapshot.md')
    raise KnowledgeError('Invalid command or arguments.\n' + HELP)


def execute_wiki(root, confirmation, settings=None):
    """Execute only the exact action accepted through the controller's review UI."""
    try:
        return _execute_wiki(root, confirmation, settings)
    except (KnowledgeError, OSError, UnicodeError, json.JSONDecodeError, sqlite3.DatabaseError) as exc:
        return {'message': f'Wiki action failed: {exc}', 'error': True}


def _execute_wiki(root, confirmation, settings):
    if not isinstance(confirmation, dict):
        raise KnowledgeError('Missing valid confirmation record')
    frozen = {k: v for k, v in confirmation.items() if k != 'payload_sha256'}
    if content_sha256(frozen) != confirmation.get('payload_sha256'):
        raise KnowledgeConflictError('Confirmation content changed; preview again')
    if frozen.get('schema_version') != '1.0' or frozen.get('workspace') != str(_root(root)):
        raise KnowledgeConflictError('Confirmation record belongs to another workspace')
    service = knowledge_service(root, settings, read_only=False)
    if frozen.get('library_root') != str(service.root) or frozen.get('shared_root') != (
            str(service.shared_root) if service.shared_root else None):
        raise KnowledgeConflictError('Knowledge configuration changed; preview again')
    operation = frozen.get('operation')
    args = frozen.get('arguments')
    if not isinstance(args, dict):
        raise KnowledgeError('Confirmation record lacks concrete arguments')
    required = {'import': {'path', 'file_sha256', 'payloads'},
                'review': {'items', 'decision'},
                'retract': {'entry_id', 'expected_sha256'},
                'publish': {'entry_id', 'expected_sha256'},
                'export': {'path', 'query', 'snapshot_sha256'}}
    if operation not in required or set(args) != required[operation]:
        raise KnowledgeError('Invalid confirmation action arguments')
    if operation == 'import':
        file_sha, payloads = _read_import(root, args['path'])
        if file_sha != args['file_sha256'] or canonical_json(payloads) != canonical_json(args['payloads']):
            raise KnowledgeConflictError('Import file changed after preview; preview again')
        proposals = service.propose_many(args['payloads'])
        pending = [p for p in proposals if p['status'] == 'pending']
        return {'message': f'Processed {len(proposals)} candidates; {len(pending)} pending review. Identical reviewed content was reused.\n'
                           'Inspect with /wiki pending; review with /wiki review <candidate-id> or /wiki review all.'}
    if operation == 'review':
        reviewed = service.review_many(args['items'], args['decision'], reviewer='user')
        action = 'approved' if args['decision'] == 'approve' else 'rejected'
        return {'message': f'{len(reviewed)} knowledge candidates {action}. Shared publication requires /wiki publish separately.'}
    if operation == 'retract':
        entry = service.retract(args['entry_id'], args['expected_sha256'])
        return {'message': f'Retracted {entry["entry_id"]}; history retained.'}
    if operation == 'publish':
        entry = service.publish(args['entry_id'], args['expected_sha256'], confirmed=True)
        return {'message': f'Published {entry["entry_id"]} to the shared library; revision {entry["revision"]}.'}
    if operation == 'export':
        directory = _safe_path(root, args['path'], 'exports', existing=False)
        service.export_snapshot(directory, args['query'], expected_sha256=args['snapshot_sha256'])
        return {'message': f'Exported knowledge snapshot: {args["path"]}/snapshot.json and snapshot.md.'}
    raise KnowledgeError('Unrecognized Wiki confirmation action')

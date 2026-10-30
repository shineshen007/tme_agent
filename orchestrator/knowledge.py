"""Reviewed, versioned scientific knowledge, backed by the Python SQLite library.

The database is authoritative; Markdown is a portable reading view. A matching
quotation proves that a passage was retrieved, not that it entails a claim.
Human review remains necessary before proposed knowledge can be reused.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit
import uuid

from .privacy import sensitive_matches


KINDS = frozenset({'statistical_fact', 'literature_report', 'biological_inference',
                   'hypothesis', 'know_how', 'experience'})
SOURCE_TYPES = frozenset({'publication', 'database', 'frozen_artifact',
                          'experiment_record', 'generated_report'})
ENTITY_TYPES = frozenset({'gene', 'protein', 'metabolite', 'metabolite_feature', 'pathway',
                          'cell_type', 'disease', 'publication', 'experiment', 'region', 'variant'})
SCHEMA_VERSION = 1


class KnowledgeError(ValueError):
    """Invalid knowledge operation; no successful state change is implied."""


class KnowledgeConflictError(KnowledgeError):
    """The content being reviewed or changed is no longer current."""


class KnowledgeIntegrityError(KnowledgeError):
    """Stored content does not match its immutable hash."""


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def content_sha256(value):
    return hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise KnowledgeError(f'{name} must be a nonempty string')
    return value.strip()


def _digest(value, name='sha256'):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value):
        raise KnowledgeError(f'{name} must be a lowercase SHA256 digest')
    return value


def _identifier(value, name):
    value = _text(value, name)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}', value):
        raise KnowledgeError(f'{name} contains invalid identifier characters')
    return value


def validate_payload(payload):
    """Validate and copy a proposal without trusting arbitrary model fields.

    Source contract: source_id, source_type, locator, sha256, primary (bool),
    optional text, quote and metadata. Primary publications/databases require a
    quote in the supplied text; publication metadata records content_scope.
    """
    if not isinstance(payload, dict):
        raise KnowledgeError('payload must be an object')
    allowed = {'entry_id', 'base_sha256', 'title', 'kind', 'statement', 'context',
               'sources', 'entities', 'relations', 'shareable', 'sharing_review'}
    unknown = set(payload) - allowed
    if unknown:
        raise KnowledgeError(f'Unknown payload fields: {sorted(unknown)}')
    try:
        value = json.loads(canonical_json(payload))
    except (TypeError, ValueError) as exc:
        raise KnowledgeError('payload must contain finite JSON values') from exc
    for key in ('title', 'statement'):
        value[key] = _text(value.get(key), key)
    if value.get('kind') not in KINDS:
        raise KnowledgeError(f'kind must be one of {sorted(KINDS)}')
    if not isinstance(value.get('context'), dict):
        raise KnowledgeError('context must be an object')
    if value['kind'] == 'know_how':
        for field in ('applicability', 'prerequisites', 'method_basis', 'common_failures', 'limitations'):
            _text(value['context'].get(field), 'Know-How context.' + field)
        if not value.get('sources'):
            raise KnowledgeError('Know-How requires attributable sources')
    for key in ('sources', 'entities', 'relations'):
        if not isinstance(value.get(key), list):
            raise KnowledgeError(f'{key} must be an array')
    if 'entry_id' in value:
        value['entry_id'] = _identifier(value['entry_id'], 'entry_id')
        if value['entry_id'].startswith(('project:', 'shared:')):
            raise KnowledgeError('entry_id cannot use a reserved scope prefix')
    if 'base_sha256' in value:
        _digest(value['base_sha256'], 'base_sha256')
        if 'entry_id' not in value:
            raise KnowledgeError('base_sha256 requires entry_id')
    value.setdefault('shareable', False)
    if not isinstance(value['shareable'], bool):
        raise KnowledgeError('shareable must be boolean')
    if 'sharing_review' in value and not isinstance(value['sharing_review'], dict):
        raise KnowledgeError('sharing_review must be an object')
    source_ids = set()
    primary = set()
    for source in value['sources']:
        if not isinstance(source, dict):
            raise KnowledgeError('Each source must be an object')
        sid = _identifier(source.get('source_id'), 'source_id')
        if sid in source_ids:
            raise KnowledgeError(f'Duplicate source_id: {sid}')
        source_ids.add(sid)
        if source.get('source_type') not in SOURCE_TYPES:
            raise KnowledgeError(f'Invalid source_type for {sid}')
        _text(source.get('locator'), f'{sid}.locator')
        _digest(source.get('sha256'), f'{sid}.sha256')
        if not isinstance(source.get('primary'), bool):
            raise KnowledgeError(f'{sid}.primary must be boolean')
        metadata = source.get('metadata', {})
        if not isinstance(metadata, dict):
            raise KnowledgeError(f'{sid}.metadata must be an object')
        if (source['source_type'] == 'generated_report' or metadata.get('derived') is True) and source['primary']:
            raise KnowledgeError('Generated reports cannot be primary evidence')
        if source['source_type'] == 'publication' and metadata.get('content_scope') not in {
                'abstract', 'full_text', 'snippet'}:
            raise KnowledgeError('Publication metadata.content_scope must describe the text read')
        if 'text' in source:
            if not isinstance(source['text'], str):
                raise KnowledgeError(f'{sid}.text must be a string')
            if hashlib.sha256(source['text'].encode('utf-8')).hexdigest() != source['sha256']:
                raise KnowledgeIntegrityError(f'{sid}: source text SHA256 mismatch')
        if 'quote' in source:
            quote = _text(source['quote'], f'{sid}.quote')
            if quote not in source.get('text', ''):
                raise KnowledgeError(f'{sid}: quote is absent from source text')
        if source['primary']:
            primary.add(source['source_type'])
            if source['source_type'] in {'publication', 'database'} and (
                    not source.get('text') or not source.get('quote')):
                raise KnowledgeError(f'{sid}: primary literature/database evidence needs text and quote')
    required = {'statistical_fact': {'frozen_artifact'},
                'literature_report': {'publication', 'database'},
                'experience': {'experiment_record'}}
    if value['kind'] in required and not (primary & required[value['kind']]):
        raise KnowledgeError(f'{value["kind"]} lacks an appropriate primary source')
    entity_ids = set()
    for entity in value['entities']:
        if not isinstance(entity, dict):
            raise KnowledgeError('Each entity must be an object')
        eid = _identifier(entity.get('id'), 'entity.id')
        if eid in entity_ids:
            raise KnowledgeError(f'Duplicate entity: {eid}')
        entity_ids.add(eid)
        if entity.get('type') not in ENTITY_TYPES:
            raise KnowledgeError('Unknown entity.type')
        parts = eid.split(':')
        if len(parts) < 2 or any(not part for part in parts):
            raise KnowledgeError('Entity ID must include namespace and accession')
        if entity['type'] in {'gene', 'protein', 'cell_type', 'region', 'variant'} and (
                len(parts) < 3 or not re.fullmatch(r'[1-9][0-9]*|unknown', parts[1])):
            raise KnowledgeError('Species-sensitive entity ID requires namespace:taxon:accession; use unknown explicitly')
        _text(entity.get('label'), 'entity.label')
        if 'aliases' in entity and (not isinstance(entity['aliases'], list) or
                                   any(not isinstance(x, str) or not x.strip() for x in entity['aliases'])):
            raise KnowledgeError('entity.aliases must be nonempty strings')
    for relation in value['relations']:
        if not isinstance(relation, dict):
            raise KnowledgeError('Each relation must be an object')
        _identifier(relation.get('subject'), 'relation.subject')
        _identifier(relation.get('object'), 'relation.object')
        predicate = _text(relation.get('predicate'), 'relation.predicate')
        if not re.fullmatch(r'[a-z][a-z0-9_]{1,63}', predicate):
            raise KnowledgeError('relation.predicate must be a lowercase relation name')
        ids = relation.get('source_ids', [])
        if not isinstance(ids, list) or any(not isinstance(s, str) or s not in source_ids for s in ids):
            raise KnowledgeError('relation.source_ids must reference this entry\'s sources')
    return value


_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
 entry_id TEXT PRIMARY KEY, current_revision INTEGER NOT NULL,
 status TEXT NOT NULL, origin TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS revisions (
 entry_id TEXT NOT NULL, revision INTEGER NOT NULL, sha256 TEXT NOT NULL,
 payload TEXT NOT NULL, reviewer TEXT NOT NULL, reviewed_at TEXT NOT NULL,
 PRIMARY KEY(entry_id, revision));
CREATE TABLE IF NOT EXISTS proposals (
 id TEXT PRIMARY KEY, entry_id TEXT NOT NULL, base_sha256 TEXT,
 sha256 TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL,
 created_at TEXT NOT NULL, reviewer TEXT, reviewed_at TEXT);
CREATE TABLE IF NOT EXISTS events (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL, entry_id TEXT,
 sha256 TEXT, actor TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS immutable_revisions_update
 BEFORE UPDATE ON revisions BEGIN SELECT RAISE(ABORT, 'Revisions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_revisions_delete
 BEFORE DELETE ON revisions BEGIN SELECT RAISE(ABORT, 'Revisions are immutable'); END;
"""


class KnowledgeService:
    """One project library plus an explicitly selected, read-only shared view.

    ``root`` and ``shared_root`` are library directories, not workspace roots.
    Construction and reads never create a database. Writes require this service
    to be writable; shared publication additionally requires explicit consent.
    """

    def __init__(self, root, shared_root=None, read_only=False):
        self.root = Path(root).expanduser().resolve()
        self.shared_root = Path(shared_root).expanduser().resolve() if shared_root else None
        if self.shared_root == self.root:
            raise KnowledgeError('Project and shared libraries must be different directories')
        self.read_only = bool(read_only)
        self.origin = hashlib.sha256(str(self.root).encode('utf-8')).hexdigest()

    @staticmethod
    def _path(root):
        return root / 'knowledge.sqlite3'

    @contextmanager
    def _connect(self, root=None, write=False):
        root = root or self.root
        path = self._path(root)
        if write:
            if self.read_only:
                raise PermissionError('KnowledgeService is read-only')
            root.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(path, timeout=15, isolation_level=None)
        else:
            if not path.exists():
                yield None
                return
            db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True,
                                 timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA busy_timeout = 15000')
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version not in ({0, SCHEMA_VERSION} if write else {SCHEMA_VERSION}):
                raise KnowledgeIntegrityError(f'Unsupported knowledge schema: {version}')
            if write:
                db.executescript(_SCHEMA)
                db.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')
                db.execute('BEGIN IMMEDIATE')
            else:
                db.execute('BEGIN')
            yield db
            if write:
                db.commit()
        except BaseException:
            if write:
                db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _event(db, action, entry_id, sha256, actor):
        db.execute('INSERT INTO events(action,entry_id,sha256,actor,created_at) VALUES(?,?,?,?,?)',
                   (action, entry_id, sha256, actor, _now()))

    @staticmethod
    def _verify(row):
        value = json.loads(row['payload'])
        if content_sha256(value) != row['sha256']:
            raise KnowledgeIntegrityError('Stored knowledge SHA256 mismatch')
        # Validate stored records as well as newly proposed data.
        validate_payload(value)
        return value

    def _entry(self, db, entry_id, scope, revision=None):
        current = db.execute('SELECT * FROM entries WHERE entry_id=?', (entry_id,)).fetchone()
        if current is None:
            return None
        rev = current['current_revision'] if revision is None else revision
        row = db.execute('SELECT * FROM revisions WHERE entry_id=? AND revision=?',
                         (entry_id, rev)).fetchone()
        if row is None:
            return None
        value = self._verify(row)
        value.update(id=entry_id, entry_id=entry_id, type=value['kind'],
                     revision=row['revision'], sha256=row['sha256'], scope=scope,
                     status=current['status'], current_revision=current['current_revision'],
                     reviewer=row['reviewer'], reviewed_at=row['reviewed_at'])
        return value

    @staticmethod
    def _proposal(row):
        if row is None:
            return None
        payload = KnowledgeService._verify(row)
        return {'id': row['id'], 'entry_id': row['entry_id'], 'sha256': row['sha256'],
                'base_sha256': row['base_sha256'], 'status': row['status'],
                'created_at': row['created_at'], 'payload': payload}

    def propose(self, payload):
        return self.propose_many([payload])[0]

    def propose_many(self, payloads):
        """Import one bounded batch atomically; no partial candidate import."""
        if not isinstance(payloads, list) or not 1 <= len(payloads) <= 100:
            raise KnowledgeError('A proposal batch must contain between 1 and 100 entries')
        values = [validate_payload(payload) for payload in payloads]
        with self._connect(write=True) as db:
            return [self._propose_value(db, value) for value in values]

    def _propose_value(self, db, value):
        base = value.pop('base_sha256', None)
        if 'entry_id' not in value:
            value['entry_id'] = 'kb_' + content_sha256(value)[:24]
        eid = value['entry_id']
        sha = content_sha256(value)
        current = self._entry(db, eid, 'project')
        if current and base is not None and base != current['sha256']:
            raise KnowledgeConflictError('base_sha256 no longer matches the current entry')
        if current and current['sha256'] == sha and current['status'] == 'reviewed':
            return {'id': None, 'entry_id': eid, 'sha256': sha, 'status': 'already_reviewed',
                    'base_sha256': sha, 'payload': value}
        if current and base != current['sha256']:
            raise KnowledgeConflictError('Updating an entry requires its current base_sha256')
        if not current and base is not None:
            raise KnowledgeConflictError('Cannot update a missing entry')
        row = db.execute("SELECT * FROM proposals WHERE entry_id=? AND sha256=? AND status='pending'",
                         (eid, sha)).fetchone()
        if row is not None and row['base_sha256'] == base:
            return self._proposal(row)
        pid = 'proposal_' + uuid.uuid4().hex
        db.execute('INSERT INTO proposals VALUES(?,?,?,?,?,?,?,?,?)',
                   (pid, eid, base, sha, canonical_json(value), 'pending', _now(), None, None))
        self._event(db, 'propose', eid, sha, 'agent')
        return self._proposal(db.execute('SELECT * FROM proposals WHERE id=?', (pid,)).fetchone())

    def pending(self):
        with self._connect() as db:
            if db is None:
                return []
            return [self._proposal(row) for row in db.execute(
                "SELECT * FROM proposals WHERE status='pending' ORDER BY created_at,id")]

    def review(self, proposal_id, expected_sha256, decision, reviewer='user'):
        return self.review_many([{'id': proposal_id, 'sha256': expected_sha256}], decision, reviewer)[0]

    def review_many(self, items, decision, reviewer='user'):
        """Review an exact frozen batch in one transaction, or change nothing."""
        if not isinstance(items, list) or not 1 <= len(items) <= 100:
            raise KnowledgeError('A review batch must contain between 1 and 100 proposals')
        for item in items:
            if not isinstance(item, dict):
                raise KnowledgeError('Review items must be objects')
            _text(item.get('id'), 'proposal_id')
            _digest(item.get('sha256'), 'expected_sha256')
        if len({item['id'] for item in items}) != len(items):
            raise KnowledgeError('Duplicate proposals in review batch')
        reviewer = _text(reviewer, 'reviewer')
        if decision not in {'approve', 'reject'}:
            raise KnowledgeError('decision must be approve or reject')
        with self._connect(write=True) as db:
            return [self._review_value(db, item['id'], item['sha256'], decision, reviewer) for item in items]

    def _review_value(self, db, proposal_id, expected_sha256, decision, reviewer):
        row = db.execute('SELECT * FROM proposals WHERE id=?', (proposal_id,)).fetchone()
        if row is None:
            raise KnowledgeError('Unknown proposal')
        if row['status'] != 'pending' or row['sha256'] != expected_sha256:
            raise KnowledgeConflictError('Proposal changed or was already reviewed')
        payload = self._verify(row)
        eid = row['entry_id']
        current = self._entry(db, eid, 'project')
        if decision == 'approve':
            if (current['sha256'] if current else None) != row['base_sha256']:
                raise KnowledgeConflictError('Entry changed after this proposal was prepared')
            rev = 1 if current is None else current['revision'] + 1
            db.execute('INSERT INTO revisions VALUES(?,?,?,?,?,?)',
                       (eid, rev, expected_sha256, canonical_json(payload), reviewer, _now()))
            db.execute('INSERT INTO entries VALUES(?,?,?,?) ON CONFLICT(entry_id) DO UPDATE SET '
                       'current_revision=excluded.current_revision,status=excluded.status',
                       (eid, rev, 'reviewed', self.origin))
        status = 'approved' if decision == 'approve' else 'rejected'
        db.execute('UPDATE proposals SET status=?,reviewer=?,reviewed_at=? WHERE id=?',
                   (status, reviewer, _now(), proposal_id))
        self._event(db, decision, eid, expected_sha256, reviewer)
        if decision == 'approve':
            return self._entry(db, eid, 'project')
        return self._proposal(db.execute('SELECT * FROM proposals WHERE id=?', (proposal_id,)).fetchone())

    def _all(self):
        result = []
        seen = set()
        for scope, root in (('project', self.root), ('shared', self.shared_root)):
            if root is None:
                continue
            with self._connect(root) as db:
                if db is None:
                    continue
                for row in db.execute("SELECT entry_id FROM entries WHERE status='reviewed' ORDER BY entry_id"):
                    if row['entry_id'] not in seen:
                        result.append(self._entry(db, row['entry_id'], scope))
                        seen.add(row['entry_id'])
        return result

    @staticmethod
    def _backlinks(entry, entries):
        targets = {entry['entry_id']} | {e['id'] for e in entry['entities']}
        links = []
        for other in entries:
            if (entry['scope'], entry['entry_id']) == (other['scope'], other['entry_id']):
                continue
            for rel in other['relations']:
                if rel['object'] in targets or rel['subject'] in targets:
                    links.append({'entry_id': other['entry_id'], 'scope': other['scope'],
                                  'revision': other['revision'], 'predicate': rel['predicate'],
                                  'subject': rel['subject'], 'object': rel['object']})
        return sorted(links, key=lambda x: canonical_json(x))

    def get(self, entry_id, revision=None):
        if revision is not None and (not isinstance(revision, int) or isinstance(revision, bool) or revision < 1):
            raise KnowledgeError('revision must be a positive integer')
        scopes = [('project', self.root), ('shared', self.shared_root)]
        if entry_id.startswith(('project:', 'shared:')):
            scope, entry_id = entry_id.split(':', 1)
            scopes = [(s, root) for s, root in scopes if s == scope]
        for scope, root in scopes:
            if root is None:
                continue
            with self._connect(root) as db:
                if db is None:
                    continue
                item = self._entry(db, entry_id, scope, revision)
                if item is not None:
                    item['backlinks'] = self._backlinks(item, self._all())
                    return item
        return None

    def search(self, query, context=None, limit=8):
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise KnowledgeError('limit must be between 1 and 100')
        return self._matching_entries(query, context)[:limit]

    def _matching_entries(self, query, context=None):
        if not isinstance(query, str):
            raise KnowledgeError('query must be a string')
        if context is not None and not isinstance(context, dict):
            raise KnowledgeError('context must be an object')
        tokens = set(re.findall(r'[\w:-]+', query.casefold()))
        all_entries = self._all()
        scored = []
        for item in all_entries:
            incompatible = False
            for key, requested in (context or {}).items():
                stored = item['context'].get(key)
                if requested in (None, '', 'unknown') or stored in (None, '', 'unknown'):
                    continue
                left = requested if isinstance(requested, list) else [requested]
                right = stored if isinstance(stored, list) else [stored]
                if not ({canonical_json(x).casefold() for x in left} &
                        {canonical_json(x).casefold() for x in right}):
                    incompatible = True
                    break
            if incompatible:
                continue
            title = item['title'].casefold()
            body = canonical_json({key: item[key] for key in ('title', 'statement', 'context', 'entities')}).casefold()
            score = sum(3 if token in title else 1 for token in tokens if token in body)
            if tokens and not score:
                continue
            item['backlinks'] = self._backlinks(item, all_entries)
            scored.append((score, item))
        scored.sort(key=lambda x: (-x[0], 0 if x[1]['scope'] == 'project' else 1,
                                   x[1]['entry_id']))
        return [item for _, item in scored]

    @staticmethod
    def _share_guard(value):
        if not value.get('shareable'):
            raise KnowledgeError('Entry was not explicitly marked shareable')
        if not value['sources']:
            raise KnowledgeError('Shared knowledge requires attributable sources')
        for source in value['sources']:
            st = source['source_type']
            if st in {'publication', 'database'}:
                url = urlsplit(source['locator'])
                if url.scheme not in {'https', 'http'} or not url.hostname or url.username or url.password:
                    raise KnowledgeError('Shared public sources require public HTTP(S) locators')
                if url.hostname.casefold() in {'localhost', '127.0.0.1', '::1'} or url.query:
                    raise KnowledgeError('Private or query-bearing source locators cannot be published')
                hostname = url.hostname.casefold()
                try:
                    address = ipaddress.ip_address(hostname)
                except ValueError:
                    if '.' not in hostname or hostname.endswith(('.local', '.internal', '.localhost')):
                        raise KnowledgeError('Shared source locator is not a public hostname')
                else:
                    if not address.is_global:
                        raise KnowledgeError('Shared source locator points to a non-public address')
            elif st == 'experiment_record':
                review = value.get('sharing_review', {})
                if (source.get('metadata', {}).get('deidentified') is not True or
                        review.get('deidentified') is not True or not review.get('reviewer') or
                        not re.fullmatch(r'experience:[A-Za-z0-9_.-]+', source['locator'])):
                    raise KnowledgeError('Shared experience requires a deidentification review and an experience locator')
            else:
                raise KnowledgeError('Private run artifacts and generated reports cannot be shared directly')
        def inspect(item):
            if isinstance(item, dict):
                for key, val in item.items():
                    if re.search(r'(^|_)(patient|donor|subject|sample|case|specimen|biospecimen)(_?id|_ids)$|'
                                 r'^(mrn|medical_record|record_number|accession|dob|date_of_birth|'
                                 r'patient_name|address|phone|email|notes|comments)$|'
                                 r'password|api_?key|token|secret|'
                                 r'^(run_id|row_key|artifact_path|raw_path|input_path|source_path)$', key, re.I):
                        raise KnowledgeError('Shared payload contains a private identifier or credential field')
                    inspect(val)
            elif isinstance(item, list):
                for val in item:
                    inspect(val)
            elif isinstance(item, str):
                if sensitive_matches(item, channel='wiki'):
                    raise KnowledgeError('Shared payload contains private, clinical or credential content')
                if re.search(r'/(?:Users|home|private|tmp|Volumes)/|(?:^|\s)[A-Za-z]:[\\/]|TCGA-[A-Z0-9]{2}-[A-Z0-9]{4}', item):
                    raise KnowledgeError('Shared payload contains a private path or sample identifier')
        inspect(value)

    def publish(self, entry_id, expected_sha256, confirmed=False):
        _digest(expected_sha256, 'expected_sha256')
        if confirmed is not True:
            raise KnowledgeError('Shared publication requires explicit confirmation')
        if self.shared_root is None:
            raise KnowledgeError('No shared library was configured')
        with self._connect(write=True) as local:
            item = self._entry(local, entry_id, 'project')
            if item is None or item['status'] != 'reviewed' or item['sha256'] != expected_sha256:
                raise KnowledgeConflictError('Only the expected current reviewed revision can be published')
            row = local.execute('SELECT * FROM revisions WHERE entry_id=? AND revision=?',
                                (entry_id, item['revision'])).fetchone()
            value = self._verify(row)
            self._share_guard(value)
            with self._connect(self.shared_root, write=True) as shared:
                existing = shared.execute('SELECT * FROM entries WHERE entry_id=?', (entry_id,)).fetchone()
                if existing:
                    previous = self._entry(shared, entry_id, 'shared')
                    if previous['sha256'] == expected_sha256 and previous['status'] == 'reviewed':
                        return previous
                    if existing['origin'] != self.origin:
                        raise KnowledgeConflictError('Shared entry belongs to a different project')
                revision = 1 if existing is None else existing['current_revision'] + 1
                shared.execute('INSERT INTO revisions VALUES(?,?,?,?,?,?)',
                               (entry_id, revision, expected_sha256, row['payload'], 'user', _now()))
                shared.execute('INSERT INTO entries VALUES(?,?,?,?) ON CONFLICT(entry_id) DO UPDATE SET '
                               'current_revision=excluded.current_revision,status=excluded.status',
                               (entry_id, revision, 'reviewed', self.origin))
                self._event(shared, 'publish', entry_id, expected_sha256, 'user')
                result = self._entry(shared, entry_id, 'shared')
            self._event(local, 'publish', entry_id, expected_sha256, 'user')
            return result

    def retract(self, entry_id, expected_sha256, reviewer='user'):
        _digest(expected_sha256, 'expected_sha256')
        reviewer = _text(reviewer, 'reviewer')
        with self._connect(write=True) as db:
            item = self._entry(db, entry_id, 'project')
            if item is None or item['sha256'] != expected_sha256:
                raise KnowledgeConflictError('Entry changed before retraction')
            # Withdraw the shared copy first. Failure leaves the local version
            # untouched and is reported; old revisions remain auditable.
            if self.shared_root and self._path(self.shared_root).exists():
                with self._connect(self.shared_root, write=True) as shared:
                    published = shared.execute('SELECT * FROM entries WHERE entry_id=?', (entry_id,)).fetchone()
                    if published and published['origin'] == self.origin:
                        shared.execute("UPDATE entries SET status='retracted' WHERE entry_id=?", (entry_id,))
                        self._event(shared, 'retract', entry_id, expected_sha256, reviewer)
            db.execute("UPDATE entries SET status='retracted' WHERE entry_id=?", (entry_id,))
            # Pending candidates prepared from this entry must not silently
            # revive it after the reviewer has withdrawn its evidence.
            db.execute("UPDATE proposals SET status='invalidated',reviewer=?,reviewed_at=? "
                       "WHERE entry_id=? AND status='pending'", (reviewer, _now(), entry_id))
            self._event(db, 'retract', entry_id, expected_sha256, reviewer)
            return self._entry(db, entry_id, 'project')

    def snapshot(self, query, context=None):
        # A snapshot/export contains every matching reviewed entry. Interactive
        # retrieval limits must not silently truncate the preserved knowledge.
        entries = self._matching_entries(query, context)
        frozen = {'schema_version': '1.0', 'query': query, 'context': context or {}, 'entries': entries}
        frozen['sha256'] = content_sha256(frozen)
        lines = ['# Scientific Wiki snapshot', '', f'Query: {query}', '',
                 f'SHA256: `{frozen["sha256"]}`', '']
        for item in entries:
            lines.extend([f'## {item["title"]}', '',
                          f'{item["kind"]} | {item["scope"]} | {item["entry_id"]} | revision {item["revision"]}', '',
                          item['statement'], '', 'Sources:'])
            lines.extend(f'- {s["source_id"]}: {s["locator"]} (SHA256 `{s["sha256"]}`)' for s in item['sources'])
            lines.append('')
        frozen['markdown'] = '\n'.join(lines)
        return frozen

    def export_snapshot(self, directory, query, context=None, expected_sha256=None):
        """Export a new immutable bundle; never overwrite an existing directory."""
        if self.read_only:
            raise PermissionError('KnowledgeService is read-only')
        frozen = self.snapshot(query, context)
        if expected_sha256 is not None and frozen['sha256'] != _digest(expected_sha256, 'expected_sha256'):
            raise KnowledgeConflictError('Knowledge changed after snapshot preparation')
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=False)
        (directory / 'snapshot.json').write_text(
            json.dumps({k: v for k, v in frozen.items() if k != 'markdown'},
                       ensure_ascii=False, sort_keys=True, indent=2) + '\n', encoding='utf-8')
        (directory / 'snapshot.md').write_text(frozen['markdown'], encoding='utf-8')
        return frozen

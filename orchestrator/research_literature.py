"""Bounded public literature retrieval and source-text verification (stdlib only).

Retrieval/quotation checks establish provenance, never semantic entailment or
novelty. Retrieved prose is untrusted data, including instructions found in it.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import html
import json
import logging
from pathlib import Path
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET

from .privacy import sensitive_matches

LOG = logging.getLogger(__name__)
PUBMED = 'https://eutils.ncbi.nlm.nih.gov/entrez/eutils/'
EUROPEPMC = 'https://www.ebi.ac.uk/europepmc/webservices/rest/'
MAX_RESPONSE_BYTES = 12 * 1024 * 1024
DEFAULTS = {'providers': ['pubmed', 'europepmc'], 'offline': False, 'refresh': False,
            'max_records': 10, 'max_queries': 8, 'fetch_full_text': False,
            'timeout': 25, 'retries': 2, 'min_interval': 0.35}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _digest(value):
    data = value if isinstance(value, bytes) else json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
    return hashlib.sha256(data).hexdigest()


def _normalise(text):
    return ' '.join(unicodedata.normalize('NFKC', html.unescape(str(text))).split())


def _plain(text):
    return _normalise(re.sub(r'<[^>]+>', ' ', str(text or '')))


def _text(element):
    return _normalise(''.join(element.itertext())) if element is not None else ''


def _passage(locator, text, kind='abstract'):
    text = _normalise(text)
    return {'locator': locator, 'kind': kind, 'text': text,
            'sha256': hashlib.sha256(text.encode()).hexdigest()}


def record_passages(record):
    """Read legacy abstract records without pretending missing text was retrieved."""
    if record.get('passages'):
        return record['passages']
    abstract = _plain(record.get('abstract', ''))
    return [_passage('abstract:1', abstract)] if abstract else []


def _seal(record):
    record['source_text_sha256'] = _digest(record.get('passages', []))
    return record


def verify_source_record(record):
    passages = record.get('passages', [])
    if not isinstance(passages, list) or not passages:
        raise ValueError('Source has no verifiable passage text')
    seen = set()
    for p in passages:
        if not isinstance(p, dict) or not isinstance(p.get('text'), str) or not p.get('locator'):
            raise ValueError('Invalid source passage')
        if p['locator'] in seen:
            raise ValueError('Duplicate source passage locator')
        seen.add(p['locator'])
        if hashlib.sha256(p['text'].encode()).hexdigest() != p.get('sha256'):
            raise ValueError('Source passage hash mismatch')
    if _digest(passages) != record.get('source_text_sha256'):
        raise ValueError('Source text snapshot hash mismatch')


def select_passages(record, query, limit_chars=2400):
    """Rank source sentences/paragraphs, keeping exact locators and source hashes.

    An excerpt can be shorter than its source paragraph. Its ``sha256`` is the
    original paragraph hash; ``excerpt_sha256`` hashes the returned text.
    """
    if not isinstance(limit_chars, int) or isinstance(limit_chars, bool) or limit_chars < 1:
        raise ValueError('limit_chars must be a positive integer')
    terms = set(re.findall(r'[A-Za-z0-9_-]{2,}|[\u3400-\u9fff]{2,}', str(query).lower()))
    ranked = []
    for index, passage in enumerate(record_passages(record)):
        text = passage['text']
        score = sum(text.lower().count(t) for t in terms)
        # Sentence selection reaches relevant content beyond a long introduction.
        sentences = re.split(r'(?<=[.!?。！？])\s+', text)
        for number, sentence in enumerate(sentences):
            local = sum(sentence.lower().count(t) for t in terms)
            ranked.append((local, score, -index, -number, passage, sentence))
    ranked.sort(key=lambda item: item[:4], reverse=True)
    selected, remaining = [], limit_chars
    for _, _, _, _, passage, sentence in ranked:
        if remaining <= 0:
            break
        if len(sentence) > remaining:
            # Center the retained span on a relevant term instead of taking the head.
            positions = [sentence.lower().find(t) for t in terms if t in sentence.lower()]
            start = max(0, min(positions) - remaining // 3) if positions else 0
            start = min(start, max(0, len(sentence) - remaining))
            sentence = sentence[start:start + remaining]
        if not sentence.strip():
            continue
        selected.append({**passage, 'text': sentence,
                         'excerpt_sha256': hashlib.sha256(sentence.encode()).hexdigest()})
        remaining -= len(sentence)
    return selected


def validate_literature_claims(claims, records, known_claim_ids=None):
    """Verify quoted text and its immutable source; semantic judgment stays pending.

    Every claim must bind to an explicitly supplied known claim ID. A valid PMID
    and a nonempty quotation alone never satisfy this contract.
    """
    if not isinstance(claims, list):
        raise ValueError('Literature claims must be a list')
    known = set(known_claim_ids or ())
    if isinstance(records, dict):
        records = records.get('records', [])
    if not isinstance(records, list) or not all(isinstance(r, dict) for r in records):
        raise ValueError('Literature records must be a list of source records')
    by = {}
    for record in records:
        sid = record.get('source_id')
        if sid in by:
            raise ValueError('Duplicate literature source ID')
        if sid:
            by[sid] = record
    validated = []
    for claim in claims:
        if (not isinstance(claim, dict) or not isinstance(claim.get('claim_id'), str)
                or claim.get('claim_id') not in known):
            provided = claim.get('claim_id') if isinstance(claim, dict) else None
            raise ValueError('Literature evidence requires a known claim_id: '
                             f'{provided!r} is not an interpretation/hypothesis ID; allowed IDs: {sorted(known)!r}. '
                             'Use the exact id of the claim being supported or contradicted, not a new citation ID.')
        if claim.get('relation') not in {'supports', 'contradicts', 'unclear'}:
            raise ValueError('Invalid literature relation')
        if claim.get('directness') not in {'direct', 'indirect', 'unknown'}:
            raise ValueError('Invalid literature directness')
        if not isinstance(claim.get('source_id'), str) or not isinstance(claim.get('quote'), str):
            raise ValueError('Literature evidence requires a source identifier and text quotation')
        record = by.get(claim.get('source_id'))
        if record is None:
            raise ValueError('Unknown literature source_id')
        verify_source_record(record)
        quote = _normalise(claim.get('quote', ''))
        if not quote:
            raise ValueError('Literature evidence requires an exact source quotation')
        matches = [p for p in record['passages'] if quote in _normalise(p['text'])
                   and (not claim.get('locator') or p['locator'] == claim['locator'])]
        if not matches:
            raise ValueError('Quotation or locator does not match source text')
        p = matches[0]
        if claim.get('source_text_sha256', record['source_text_sha256']) != record['source_text_sha256']:
            raise ValueError('Claim source hash mismatch')
        if claim.get('passage_sha256', p['sha256']) != p['sha256']:
            raise ValueError('Claim passage hash mismatch')
        validated.append({**claim, 'quote': quote, 'locator': p['locator'],
                          'passage_sha256': p['sha256'],
                          'source_text_sha256': record['source_text_sha256'],
                          'text_verified': True, 'semantic_review': 'required'})
    return validated


def _record(provider, *, pmid='', pmcid='', doi='', external_id='', **metadata):
    doi = str(doi or '').removeprefix('https://doi.org/').lower().strip()
    pmid = str(pmid or '')
    if pmid and not re.fullmatch(r'\d+', pmid):
        raise ValueError('Provider returned an invalid PMID')
    source_id = 'PMID:' + pmid if pmid else 'DOI:' + doi if doi else 'EPMC:' + str(external_id)
    return {'source_id': source_id, 'pmid': pmid, 'pmcid': str(pmcid or ''), 'doi': doi,
            'url': f'https://pubmed.ncbi.nlm.nih.gov/{pmid}/' if pmid else
                   f'https://doi.org/{doi}' if doi else 'https://europepmc.org/article/' + str(external_id),
            'providers': [provider], 'retrieved_at': _now(), 'query_ids': [],
            'passages': [], 'publication_types': [], 'retraction_metadata': [], **metadata}


def _merge_records(records):
    """Union connected PMID/DOI/PMCID records, retaining all source passages."""
    merged = []
    for source in records:
        source = json.loads(json.dumps(source))
        keys = {f'{k}:{source[k]}' for k in ('pmid', 'doi', 'pmcid') if source.get(k)}
        matches = [r for r in merged if r['source_id'] == source['source_id'] or keys &
                   {f'{k}:{r[k]}' for k in ('pmid', 'doi', 'pmcid') if r.get(k)}]
        for existing in matches:
            merged.remove(existing)
            for name in ('pmid', 'pmcid', 'doi', 'abstract', 'title', 'year', 'journal'):
                source[name] = source.get(name) or existing.get(name, '')
            for name in ('providers', 'query_ids', 'publication_types', 'retraction_metadata', 'authors'):
                source[name] = list({json.dumps(v, sort_keys=True): v for v in
                                     [*existing.get(name, []), *source.get(name, [])]}.values())
            passages = {p['locator']: p for p in existing.get('passages', [])}
            for p in source.get('passages', []):
                if p['locator'] in passages and passages[p['locator']]['sha256'] != p['sha256']:
                    p = {**p, 'locator': p['locator'] + ':' + p['sha256'][:12]}
                passages[p['locator']] = p
            source['passages'] = list(passages.values())
        source['source_id'] = ('PMID:' + source['pmid'] if source.get('pmid') else
                               'DOI:' + source['doi'] if source.get('doi') else source['source_id'])
        merged.append(_seal(source))
    return sorted(merged, key=lambda r: r['source_id'])


def _public_queries(queries, context, maximum):
    context = context or {}
    if not isinstance(context, dict) or set(context) - {'species', 'disease', 'assay', 'public_terms', 'private_ids'}:
        raise ValueError('Literature context accepts public research terms and a local private-ID denylist only')
    private = context.get('private_ids', [])
    if not isinstance(private, (list, tuple, set)) or not all(isinstance(v, str) and v for v in private):
        raise ValueError('Invalid local private-ID denylist')
    for key in ('species', 'disease', 'assay'):
        if key in context and (not isinstance(context[key], str) or len(context[key]) > 300):
            raise ValueError('Research context must contain bounded public text, not raw data')
    if 'public_terms' in context and (not isinstance(context['public_terms'], list) or
            not all(isinstance(t, str) and len(t) <= 300 for t in context['public_terms'])):
        raise ValueError('public_terms must contain bounded text terms')
    public_values = [context[k] for k in ('species', 'disease', 'assay') if k in context]
    public_values.extend(context.get('public_terms', []))
    if any(pid.lower() in value.lower() for pid in private for value in public_values):
        raise ValueError('Public research context contains a private identifier')
    if any(sensitive_matches(value, private, channel='literature') for value in public_values):
        raise ValueError('Public research context contains patient, sample or clinical free-text identifiers')
    if not isinstance(queries, list) or len(queries) > maximum:
        raise ValueError(f'queries must be a list with at most {maximum} public queries')
    result = []
    for item in queries:
        query = item.get('query') if isinstance(item, dict) else item
        if not isinstance(query, str) or not query.strip() or len(query) > 1200:
            raise ValueError('Invalid public literature query')
        query = query.strip()
        if any(pid.lower() in query.lower() for pid in private) or re.search(
                r'\bTCGA-[A-Z0-9]{2}-[A-Z0-9]{4}(?:-[A-Z0-9]+)?\b|'
                r'\b(?:patient|sample|subject|donor)(?:[:=][A-Za-z0-9_-]+|'
                r'[_ -](?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]+)|'
                r'[\w.+-]+@[\w.-]+\.[A-Za-z]+|(?:/Users/|/home/|[A-Za-z]:\\)|[\t\n\r]', query, re.I):
            raise ValueError('Query contains private identifiers, local paths, or raw data')
        if sensitive_matches(query, private, channel='literature'):
            raise ValueError('Query contains patient, sample or clinical free-text identifiers')
        if query not in result:
            result.append(query)
    return result, {k: v for k, v in context.items() if k != 'private_ids'}


def _check_url(url):
    parsed = urllib.parse.urlparse(url)
    if (parsed.scheme != 'https' or parsed.hostname not in {'eutils.ncbi.nlm.nih.gov', 'www.ebi.ac.uk'}
            or parsed.port not in {None, 443} or parsed.username or parsed.password or parsed.fragment):
        raise ValueError('Literature transport only accepts approved public HTTPS endpoints')


def _http(url, timeout):
    class Redirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            _check_url(newurl)
            return super().redirect_request(req, fp, code, msg, headers, newurl)
    request = urllib.request.Request(url, headers={'User-Agent': 'tme-agent-research/1.0'})
    with urllib.request.build_opener(Redirect()).open(request, timeout=timeout) as response:
        _check_url(response.url)
        content = response.read(MAX_RESPONSE_BYTES + 1)
    if len(content) > MAX_RESPONSE_BYTES:
        raise ValueError('Literature response exceeds size limit')
    return content


class LiteratureService:
    def __init__(self, cache_dir, settings=None, transport=None):
        self.cache_dir = Path(cache_dir)
        self.settings = {**DEFAULTS, **(settings or {})}
        s = self.settings
        if not isinstance(s['providers'], list) or not s['providers'] or set(s['providers']) - {'pubmed', 'europepmc'}:
            raise ValueError('Supported literature providers are pubmed and europepmc')
        for name, lo, hi in (('max_records', 1, 100), ('max_queries', 1, 32), ('retries', 0, 4)):
            if not isinstance(s[name], int) or isinstance(s[name], bool) or not lo <= s[name] <= hi:
                raise ValueError(f'Invalid literature setting: {name}')
        if not 0 < float(s['timeout']) <= 120 or not 0 <= float(s['min_interval']) <= 10:
            raise ValueError('Invalid literature timeout or rate limit')
        self.transport = transport or _http
        self._last_request = 0.0

    def _save(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
        temp.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2), encoding='utf-8')
        temp.replace(path)

    def _get(self, url, snapshots):
        _check_url(url)
        for attempt in range(self.settings['retries'] + 1):
            # Enforce NCBI's unauthenticated rate even if a user lowers the setting.
            interval = max(float(self.settings['min_interval']), 0.34) if self.transport is _http else float(self.settings['min_interval'])
            delay = interval - (time.monotonic() - self._last_request)
            if delay > 0:
                time.sleep(delay)
            self._last_request = time.monotonic()
            try:
                content = self.transport(url, timeout=self.settings['timeout'])
                content = content.encode() if isinstance(content, str) else content
                if not isinstance(content, bytes) or len(content) > MAX_RESPONSE_BYTES:
                    raise ValueError('Invalid or oversized literature response')
                digest = _digest(content)
                target = self.cache_dir / 'responses' / (digest + '.bin')
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    try:
                        with target.open('xb') as stream:
                            stream.write(content)
                    except FileExistsError:
                        pass
                if _digest(target.read_bytes()) != digest:
                    raise ValueError('Cached response hash mismatch')
                snapshots.append({'url': url, 'sha256': digest, 'path': str(target), 'retrieved_at': _now()})
                return content
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                retryable = not isinstance(exc, urllib.error.HTTPError) or exc.code == 429 or exc.code >= 500
                if not retryable or attempt == self.settings['retries']:
                    raise
                # Never log URLs or transport bodies from arbitrary errors.
                LOG.warning('Literature request retry: %s', type(exc).__name__)
                time.sleep(min(2 ** attempt, 4))

    def _pubmed(self, query, snapshots, errors):
        args = urllib.parse.urlencode({'db': 'pubmed', 'tool': 'tme-agent', 'term': query, 'retmode': 'json',
                                      'retmax': self.settings['max_records'], 'sort': 'relevance'})
        search = json.loads(self._get(PUBMED + 'esearch.fcgi?' + args, snapshots))['esearchresult']
        if search.get('errorlist'):
            raise ValueError('PubMed rejected one or more search terms')
        ids = search.get('idlist', [])
        total = int(search['count'])
        if not isinstance(ids, list) or any(not re.fullmatch(r'\d+', str(v)) for v in ids):
            raise ValueError('Invalid PubMed search identifiers')
        if total < 0 or len(ids) > self.settings['max_records']:
            raise ValueError('PubMed response violates the bounded query contract')
        if not ids:
            return [], total
        args = urllib.parse.urlencode({'db': 'pubmed', 'tool': 'tme-agent', 'id': ','.join(ids), 'retmode': 'xml'})
        tree = ET.fromstring(self._get(PUBMED + 'efetch.fcgi?' + args, snapshots))
        if tree.tag != 'PubmedArticleSet':
            raise ValueError('Invalid PubMed article response')
        records = []
        for article in tree.findall('PubmedArticle'):
            pmid = article.findtext('./MedlineCitation/PMID', '')
            if pmid not in ids:
                raise ValueError('PubMed returned an unrequested article')
            ids_by_type = {x.get('IdType'): _text(x) for x in article.findall('./PubmedData/ArticleIdList/ArticleId')}
            date = article.find('./MedlineCitation/Article/Journal/JournalIssue/PubDate')
            year = (date.findtext('Year', '') or date.findtext('MedlineDate', '')) if date is not None else ''
            record = _record('pubmed', pmid=pmid, pmcid=ids_by_type.get('pmc', ''), doi=ids_by_type.get('doi', ''),
                             title=_text(article.find('./MedlineCitation/Article/ArticleTitle')),
                             year=(re.search(r'\b\d{4}\b', year).group() if re.search(r'\b\d{4}\b', year) else ''),
                             journal=article.findtext('./MedlineCitation/Article/Journal/Title', ''),
                             authors=[_text(a.find('CollectiveName')) or ' '.join(filter(None, [a.findtext('ForeName'), a.findtext('LastName')]))
                                      for a in article.findall('./MedlineCitation/Article/AuthorList/Author')])
            record['passages'] = [_passage(f'pubmed:abstract:{i + 1}', _text(p))
                                  for i, p in enumerate(article.findall('./MedlineCitation/Article/Abstract/AbstractText')) if _text(p)]
            record['abstract'] = '\n'.join(p['text'] for p in record['passages'])
            record['publication_types'] = [_text(p) for p in article.findall('./MedlineCitation/Article/PublicationTypeList/PublicationType')]
            record['retraction_metadata'] = [{'relation': p.get('RefType', ''), 'pmid': p.findtext('PMID', ''),
                                               'reference': p.findtext('RefSource', '')}
                                              for p in article.findall('./MedlineCitation/CommentsCorrectionsList/CommentsCorrections')]
            records.append(_seal(record))
        missing = sorted(set(ids) - {r['pmid'] for r in records})
        if missing:
            errors.append({'stage': 'metadata', 'error': 'Missing requested PubMed records', 'ids': missing})
        return records, total

    def _europepmc(self, query, snapshots, errors):
        args = urllib.parse.urlencode({'query': query, 'format': 'json', 'resultType': 'core',
                                      'pageSize': self.settings['max_records']})
        payload = json.loads(self._get(EUROPEPMC + 'search?' + args, snapshots))
        total = int(payload['hitCount'])
        if total < 0 or len(payload['resultList']['result']) > self.settings['max_records']:
            raise ValueError('Europe PMC response violates the bounded query contract')
        records = []
        for raw in payload['resultList']['result']:
            external_id = str(raw.get('source', '')) + '/' + str(raw['id'])
            record = _record('europepmc', pmid=raw.get('pmid', raw['id'] if raw.get('source') == 'MED' else ''),
                             pmcid=raw.get('pmcid', ''), doi=raw.get('doi', ''), external_id=external_id,
                             title=_plain(raw.get('title', '')), year=str(raw.get('pubYear', '')),
                             journal=raw.get('journalInfo', {}).get('journal', {}).get('title', raw.get('journalTitle', '')),
                             authors=[a.get('fullName', '') for a in raw.get('authorList', {}).get('author', [])])
            record['abstract'] = _plain(raw.get('abstractText', ''))
            if record['abstract']:
                record['passages'].append(_passage('europepmc:abstract:1', record['abstract']))
            record['publication_types'] = raw.get('pubTypeList', {}).get('pubType', [])
            record['retraction_metadata'] = [{'field': k, 'value': raw[k]} for k in
                                              ('isRetracted', 'commentCorrectionList', 'versionNumber') if k in raw]
            record['is_open_access'] = raw.get('isOpenAccess') == 'Y'
            if self.settings['fetch_full_text'] and record['is_open_access'] and record['pmcid']:
                if not re.fullmatch(r'PMC\d+', record['pmcid']):
                    raise ValueError('Invalid Europe PMC full text identifier')
                try:
                    tree = ET.fromstring(self._get(EUROPEPMC + record['pmcid'] + '/fullTextXML', snapshots))
                    full = [_passage(f'europepmc:body:p:{i + 1}', _text(p), 'full_text')
                            for i, p in enumerate(tree.findall('.//body//p')) if _text(p)]
                    if not full:
                        raise ValueError('Open-access response has no body paragraphs')
                    record['passages'].extend(full)
                    record['license'] = [_text(n) for n in tree.findall('.//permissions/license')]
                except (ValueError, ET.ParseError, OSError) as exc:
                    errors.append({'stage': 'full_text', 'source_id': record['source_id'], 'error': type(exc).__name__})
            records.append(_seal(record))
        if total and not records:
            errors.append({'stage': 'metadata', 'error': 'Nonzero hit count but no records returned'})
        return records, total

    def search(self, queries, context=None):
        queries, public_context = _public_queries(queries, context, self.settings['max_queries'])
        all_records, query_results, errors = [], [], []
        for query in queries:
            for provider in dict.fromkeys(self.settings['providers']):
                query_id = _digest({'provider': provider, 'query': query})[:20]
                key = _digest({'version': 1, 'provider': provider, 'query': query,
                               'max_records': self.settings['max_records'], 'full_text': self.settings['fetch_full_text']})
                cache = self.cache_dir / 'queries' / (key + '.json')
                item = None
                if cache.exists() and (self.settings['offline'] or not self.settings['refresh']):
                    try:
                        cached = json.loads(cache.read_text(encoding='utf-8'))
                        signature = cached.pop('snapshot_sha256')
                        if signature != _digest(cached) or cached.get('query') != query or cached.get('provider') != provider:
                            raise ValueError('Query cache integrity mismatch')
                        for snapshot in cached.get('response_snapshots', []):
                            raw_path = self.cache_dir / 'responses' / (snapshot['sha256'] + '.bin')
                            if _digest(raw_path.read_bytes()) != snapshot['sha256']:
                                raise ValueError('Cached raw response hash mismatch')
                        item = {**cached, 'snapshot_sha256': signature, 'cache_hit': True}
                    except (OSError, ValueError, KeyError) as exc:
                        errors.append({'query_id': query_id, 'provider': provider, 'stage': 'cache', 'error': type(exc).__name__})
                if item is None and self.settings['offline']:
                    item = {'query_id': query_id, 'query': query, 'provider': provider, 'status': 'offline',
                            'records': [], 'errors': [], 'total_hits': None, 'cache_hit': False}
                if item is None:
                    item = {'query_id': query_id, 'query': query, 'provider': provider, 'retrieved_at': _now(),
                            'records': [], 'errors': [], 'response_snapshots': [], 'total_hits': None}
                    try:
                        records, total = getattr(self, '_' + provider)(query, item['response_snapshots'], item['errors'])
                        for record in records:
                            record['query_ids'] = [query_id]
                        item.update(records=records, total_hits=total,
                                    truncated=total > len(records), status='partial' if item['errors'] else 'complete' if records else 'no_results')
                    except (ValueError, KeyError, TypeError, ET.ParseError, OSError) as exc:
                        item['status'] = 'failed'
                        item['errors'].append({'stage': 'retrieval', 'error': type(exc).__name__})
                    item['snapshot_sha256'] = _digest(item)
                    self._save(self.cache_dir / 'snapshots' / (item['snapshot_sha256'] + '.json'), item)
                    if item['status'] != 'failed':
                        self._save(cache, item)
                    item['cache_hit'] = False
                all_records.extend(item['records'])
                errors.extend({**e, 'query_id': query_id, 'provider': provider} for e in item['errors'])
                query_results.append({k: v for k, v in item.items() if k != 'records'})
        states = [q['status'] for q in query_results]
        if self.settings['offline']:
            status = 'offline'
        elif not states or all(s == 'no_results' for s in states):
            status = 'no_results'
        elif all(s == 'failed' for s in states):
            status = 'failed'
        elif errors or any(s in {'failed', 'partial'} for s in states):
            status = 'partial'
        else:
            status = 'complete'
        result = {'schema_version': '1.0', 'status': status, 'records': _merge_records(all_records),
                  'queries': query_results, 'errors': errors, 'context': public_context,
                  'accessed_at': _now(), 'search_scope': {'providers': list(dict.fromkeys(self.settings['providers'])),
                  'max_records_per_query': self.settings['max_records'], 'full_text_requested': self.settings['fetch_full_text']},
                  'novelty_status': 'not_assessed', 'semantic_review': 'required'}
        result['snapshot_sha256'] = _digest(result)
        self._save(self.cache_dir / 'searches' / (result['snapshot_sha256'] + '.json'), result)
        return result

"""PubMed metadata retrieval for the multi-omics agent layer.

Two modes:
  1. Legacy one-off:  python scripts/04_literature.py <project_root>
     Reproduces the original behaviour (8 axis queries -> knowledge/literature/pubmed_candidates.json,
     refusing to overwrite an existing file).
  2. Job mode:        python scripts/04_literature.py <job.json>
     Deterministic, cache-backed retrieval for a run. The caller (orchestrator/literature.py)
     supplies the query list; this tool never lets a model issue network requests directly.

Guarantees: PMID metadata is verified against the response; every record keeps
stance='unreviewed' (semantic stance is never asserted here); the response cache lives
outside the source-fingerprinted directories so a run cannot break its own fingerprint.
"""
from __future__ import annotations
import csv
import hashlib
import json
import logging
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

LOG = logging.getLogger('literature')

LEGACY_QUERIES = {
    'tryptophan-kynurenine': 'hepatocellular carcinoma kynurenine immune',
    'lactate': 'hepatocellular carcinoma lactate immune',
    'cholesterol-lipid': 'hepatocellular carcinoma cholesterol immune',
    'arginine': 'hepatocellular carcinoma arginine immune',
    'nucleotide': 'hepatocellular carcinoma adenosine immune',
    'glutamine': 'hepatocellular carcinoma glutamine immune',
    'antioxidant': 'hepatocellular carcinoma glutathione immune',
    'catecholamine-3mt': 'hepatocellular carcinoma methoxytyramine immune',
}


def _fetch(url, timeout=45):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read()


def search_and_fetch(query, retmax, base):
    ids_url = base + 'esearch.fcgi?' + urllib.parse.urlencode(
        {'db': 'pubmed', 'term': query, 'retmax': retmax, 'retmode': 'json'})
    ids = json.loads(_fetch(ids_url))['esearchresult']['idlist']
    if not ids:
        return []
    time.sleep(0.4)
    fetch_url = base + 'efetch.fcgi?' + urllib.parse.urlencode(
        {'db': 'pubmed', 'id': ','.join(ids), 'retmode': 'xml'})
    tree = ET.fromstring(_fetch(fetch_url))
    records = []
    for article in tree.findall('.//PubmedArticle'):
        pmid = article.findtext('.//MedlineCitation/PMID')
        if pmid not in ids:
            raise ValueError(f'PubMed response ID mismatch: {pmid}')
        title = ''.join(article.find('.//ArticleTitle').itertext()) if article.find('.//ArticleTitle') is not None else ''
        abstract = ' '.join(''.join(x.itertext()) for x in article.findall('.//AbstractText'))
        records.append({'pmid': str(pmid), 'title': title, 'abstract': abstract,
                        'url': f'https://pubmed.ncbi.nlm.nih.gov/{pmid}/',
                        'metadata_verified': True, 'stance': 'unreviewed',
                        'interpretation': 'Retrieval match only; not evidence that this paper supports the hypothesis'})
    return records


def cached_search(query, retmax, base, cache_dir, pause):
    cache_dir = Path(cache_dir)
    key = hashlib.sha256(f'{query}|{retmax}|{base}'.encode()).hexdigest()[:32]
    path = cache_dir / f'{key}.json'
    if path.exists():
        payload = json.loads(path.read_text(encoding='utf-8'))
        return payload['records'], True
    records = search_and_fetch(query, retmax, base)
    cache_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'query': query, 'retmax': retmax, 'fetched_at': datetime.now(timezone.utc).isoformat(),
                                'records': records}, ensure_ascii=False, indent=1), encoding='utf-8')
    time.sleep(pause)
    return records, False


def run_job(job):
    out = Path(job['output_dir'])
    out.mkdir(parents=True, exist_ok=True)
    cache_dir = Path(job['project_root']) / job.get('cache_dir', '.cache/literature')
    base = job.get('eutils_base', 'https://eutils.ncbi.nlm.nih.gov/entrez/eutils/')
    retmax = int(job.get('retmax_per_query', 5))
    pause = float(job.get('request_pause_seconds', 0.4))
    records, log_rows, errors = [], [], []
    for item in job['queries']:
        query = item['query']
        try:
            hit, cached = cached_search(query, retmax, base, cache_dir, pause)
            for record in hit:
                records.append({**record, 'axis': item.get('axis', ''), 'query': query,
                                'query_source': item.get('source', 'deterministic')})
            log_rows.append({'query': query, 'axis': item.get('axis', ''), 'source': item.get('source', 'deterministic'),
                             'n_records': len(hit), 'cache_hit': cached,
                             'accessed_at': datetime.now(timezone.utc).isoformat()})
        except Exception as exc:  # retrieval failure is recorded, never silently dropped
            LOG.exception('PubMed retrieval failed for %s', query)
            errors.append({'query': query, 'error': str(exc)})
            log_rows.append({'query': query, 'axis': item.get('axis', ''), 'source': item.get('source', 'deterministic'),
                             'n_records': 0, 'cache_hit': False, 'failed': True, 'error': str(exc)})
    (out / 'records.json').write_text(json.dumps(
        {'status': 'partial' if errors else 'retrieved_requires_semantic_review',
         'mode': job.get('mode', 'base'), 'accessed_at': datetime.now(timezone.utc).isoformat(),
         'queries': job['queries'], 'records': records, 'errors': errors}, ensure_ascii=False, indent=1), encoding='utf-8')
    with (out / 'query_log.csv').open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=['query', 'axis', 'source', 'n_records', 'cache_hit',
                                                   'failed', 'error', 'accessed_at'])
        writer.writeheader()
        for row in log_rows:
            writer.writerow({k: row.get(k, '') for k in writer.fieldnames})
    print({'mode': job.get('mode', 'base'), 'records': len(records), 'errors': len(errors),
           'cache_misses': sum(1 for r in log_rows if not r['cache_hit'])})


def run_legacy(project_root):
    out = Path(project_root) / 'knowledge/literature/pubmed_candidates.json'
    if out.exists():
        raise SystemExit('Output exists; refusing overwrite')
    base = 'https://eutils.ncbi.nlm.nih.gov/entrez/eutils/'
    records, errors = [], []
    for axis, query in LEGACY_QUERIES.items():
        try:
            for record in search_and_fetch(query, 3, base):
                records.append({**record, 'axis': axis, 'query': query})
            time.sleep(0.4)
        except Exception as exc:
            LOG.exception('PubMed retrieval failed for %s', axis)
            errors.append({'axis': axis, 'error': str(exc)})
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({'status': 'partial' if errors else 'retrieved_requires_semantic_review',
                               'accessed_at': datetime.now(timezone.utc).isoformat(), 'queries': LEGACY_QUERIES,
                               'records': records, 'errors': errors}, ensure_ascii=False, indent=2) + '\n',
                   encoding='utf-8')
    print({'records': len(records), 'errors': len(errors)})


def main(argv):
    if len(argv) != 2:
        raise SystemExit('Usage: 04_literature.py <project_root | job.json>')
    target = Path(argv[1])
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    if target.suffix == '.json':
        run_job(json.loads(target.read_text(encoding='utf-8')))
    else:
        run_legacy(target)


if __name__ == '__main__':
    main(sys.argv)

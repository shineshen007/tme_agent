"""Freeze human pathway membership to prevent non-human reference pathways entering HCC ORA."""
import sys, urllib.request, hashlib, json
from pathlib import Path
from datetime import datetime, timezone
out=Path(sys.argv[1]).resolve()/'knowledge/resources/kegg_human_pathways.tsv'
if out.exists(): raise SystemExit('Output exists; refusing overwrite')
url='https://rest.kegg.jp/list/pathway/hsa'
with urllib.request.urlopen(url,timeout=90) as response: data=response.read()
rows=data.decode().splitlines()
if len(rows)<100 or not all(r.startswith('hsa') for r in rows): raise ValueError('Unexpected response')
out.write_bytes(data)
out.with_suffix('.provenance.json').write_text(json.dumps({'url':url,'accessed_at':datetime.now(timezone.utc).isoformat(),'sha256':hashlib.sha256(data).hexdigest(),'rows':len(rows)})+'\n')

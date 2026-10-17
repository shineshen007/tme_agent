"""Download public compound-pathway links only; save URL/time/hash provenance."""
from pathlib import Path
import sys
import urllib.request
import hashlib
import json
from datetime import datetime,timezone
root=Path(sys.argv[1]).resolve()
out=root/'knowledge/resources/kegg_compound_pathway.tsv'
if out.exists():
    raise SystemExit('Output exists; refusing overwrite')
url='https://rest.kegg.jp/link/compound/pathway'
with urllib.request.urlopen(url,timeout=90) as response:
    data=response.read()
lines=[line for line in data.decode().splitlines() if line.startswith('path:map')]
if len(lines)<100:
    raise ValueError('Unexpected KEGG response')
data=('\n'.join(lines)+'\n').encode()
out.write_bytes(data)
out.with_suffix('.provenance.json').write_text(json.dumps({'url':url,'accessed_at':datetime.now(timezone.utc).isoformat(),
    'sha256':hashlib.sha256(data).hexdigest(),'rows':len(lines),'scope':'reference compound-pathway associations; not reaction direction or enzyme activity'},indent=2)+'\n')

"""Machine-checkable output contracts; all observed values remain local."""
from __future__ import annotations
import csv
import hashlib
import json
import math
import subprocess
import sys
import uuid
from pathlib import Path

FORMATS={'.csv':{'csv'},'.tsv':{'tsv'},'.rds':{'rds_matrix','rds_table','rds_list'},
         '.pdf':{'pdf'},'.json':{'json'},'.txt':{'text'},'.md':{'text'},'.svg':{'svg'}}


def validate_contracts(contracts, step, required=False):
    for c in contracts:
        if 'format' not in c and not required: continue
        if c.get('format') not in FORMATS.get(Path(c['path']).suffix.lower(),set()):
            raise ValueError('Output contract needs a format matching its path: '+c['path'])
        fmt=c['format']
        field='columns' if fmt in {'csv','tsv','rds_table'} else 'keys' if fmt=='rds_list' else None
        if field and (not isinstance(c.get(field),list) or not c[field] or
                      not all(isinstance(v,str) and v for v in c[field]) or len(set(c[field]))!=len(c[field])):
            raise ValueError(f'{fmt} contract requires distinct nonempty {field}')
        if fmt=='rds_matrix' and (not isinstance(c.get('axes'),dict) or set(c['axes'])!={'rows','columns'}):
            raise ValueError('RDS matrix contract requires rows/columns axis meanings')
        for name in ('unique_by','nonmissing','finite'):
            if name in c and (not isinstance(c[name],list) or not set(c[name])<=set(c.get('columns',[]))):
                raise ValueError(f'{name} must name declared columns')
        if 'binding_index' in c:
            if not isinstance(c['binding_index'],int) or isinstance(c['binding_index'],bool) or c['binding_index']<0:
                raise ValueError('binding_index must be a nonnegative integer')
            if fmt=='rds_matrix' and c.get('sample_axis') not in {'rows','columns'}:
                raise ValueError('Bound matrices require sample_axis')
            if fmt in {'csv','tsv'} and c.get('sample_key') not in c['columns']:
                raise ValueError('Bound sample tables require sample_key')
            for key in ('group_key','pair_key'):
                if key in c and c[key] not in c.get('columns',[]): raise ValueError(f'{key} must name a declared column')


def _as_list(v):
    return v if isinstance(v,list) else ([] if v is None else [v])


def binding_data(root, plan, index):
    matrices=plan.get('input_bindings',{}).get('matrices',[])
    if index>=len(matrices): raise ValueError('Unknown output binding_index')
    b=matrices[index]
    ref=plan['input_inspection']; path=root/ref['path']
    if hashlib.sha256(path.read_bytes()).hexdigest()!=ref['sha256']: raise ValueError('Inspection changed during execution')
    profiles=json.loads(path.read_text())['profiles']
    m=next(p for p in profiles if p['path']==b['metadata_path'])
    ids=_as_list(m['keys']['column:'+b['sample_key']]['values'])
    groups=_as_list(m['keys']['column:'+b['group_key']]['values'])
    decode={hashlib.sha256(k.encode()).hexdigest():v for k,v in b['group_map'].items()}
    pairs=_as_list(m['keys']['column:'+b['pair_key']]['values']) if b['pair_key'] else [None]*len(ids)
    return {i:(decode[g],p) for i,g,p in zip(ids,groups,pairs)}


def verify_output(root, task_dir, relative, contract, env, plan):
    path=task_dir/relative; fmt=contract['format']
    bound=binding_data(root,plan,contract['binding_index']) if 'binding_index' in contract else None
    if fmt in {'csv','tsv'}:
        with path.open(encoding='utf-8-sig',newline='') as stream:
            reader=csv.DictReader(stream,delimiter='\t' if fmt=='tsv' else ',')
            if not reader.fieldnames or len(set(reader.fieldnames))!=len(reader.fieldnames) or not set(contract['columns'])<=set(reader.fieldnames):
                raise ValueError('Missing or duplicate output columns: '+relative)
            seen=set(); samples=set(); count=0
            for row in reader:
                count+=1
                if None in row or any(v is None for v in row.values()): raise ValueError('Ragged output table: '+relative)
                if any(not row[k].strip() or row[k].strip().lower() in {'na','nan','null'} for k in contract.get('nonmissing',[])):
                    raise ValueError('Missing required output value: '+relative)
                for k in contract.get('finite',[]):
                    if not math.isfinite(float(row[k])): raise ValueError('Nonfinite output: '+relative)
                if contract.get('unique_by'):
                    key=tuple(row[k] for k in contract['unique_by'])
                    if key in seen: raise ValueError('Duplicate output key: '+relative)
                    seen.add(key)
                if bound is not None:
                    key=hashlib.sha256(row[contract['sample_key']].encode()).hexdigest()
                    if key not in bound: raise ValueError('Unexpected output sample: '+relative)
                    samples.add(key); group,pair=bound[key]
                    if contract.get('group_key') and row[contract['group_key']]!=group:
                        raise ValueError('Output group disagrees with frozen metadata: '+relative)
                    if contract.get('pair_key') and hashlib.sha256(row[contract['pair_key']].encode()).hexdigest()!=pair:
                        raise ValueError('Output pairing disagrees with frozen metadata: '+relative)
            if count==0 and not contract.get('allow_empty',False): raise ValueError('Empty output table: '+relative)
            if bound is not None and samples!=set(bound): raise ValueError('Output silently dropped samples: '+relative)
        return {'status':'passed','rows':count}
    if fmt.startswith('rds_'):
        from .actions import rscript_path
        folder=task_dir/'tmp'/('verify_'+uuid.uuid4().hex); folder.mkdir()
        script=folder/'probe.R'; script.write_bytes(Path(__file__).with_name('output_probe.R').read_bytes())
        request=folder/'request.json'; result=folder/'result.json'
        payload={'path':str(path),'contract':contract}
        if bound is not None: payload['sample_hashes']=list(bound)
        request.write_text(json.dumps(payload),encoding='utf-8')
        argv=[sys.executable,str(Path(__file__).with_name('task_guard.py')),str(task_dir),'--',rscript_path(root),'--vanilla',str(script),str(request),str(result)]
        proc=subprocess.run(argv,cwd=root,env=env,capture_output=True,text=True,timeout=60)
        (folder/'probe.log').write_text(proc.stdout+proc.stderr,encoding='utf-8')
        if proc.returncode or not result.exists(): raise ValueError('RDS contract validation failed: '+relative+'; see '+str(folder/'probe.log'))
        return json.loads(result.read_text())
    if fmt=='pdf':
        import pypdf
        reader=pypdf.PdfReader(path,strict=True)
        if not len(reader.pages): raise ValueError('Empty PDF: '+relative)
        return {'status':'passed','pages':len(reader.pages)}
    if fmt=='json': json.loads(path.read_text(encoding='utf-8'))
    elif fmt=='svg':
        import xml.etree.ElementTree as ET
        if not ET.parse(path).getroot().tag.endswith('svg'): raise ValueError('Invalid SVG')
    else: path.read_text(encoding='utf-8')
    return {'status':'passed'}

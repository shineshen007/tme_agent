"""Trusted, sandboxed local structure discovery and source-bound input roles."""
from __future__ import annotations
import hashlib
import json
import re
import subprocess
import sys
import uuid
from pathlib import Path

VERSION = 'input_probe_v2'
SUPPORTED = {'.csv', '.tsv', '.rds', '.gmt'}


def _run_guarded(root: Path, folder: Path, argv: list[str], env: dict):
    """Run one guarded child process.

    Named seam: native AppContainer/Landlock/Seatbelt behaviour is exercised by
    bootstrap and native acceptance, while unit tests can substitute this call.
    """
    return subprocess.run(argv, cwd=root, env=env, capture_output=True, text=True, timeout=120)


def inspect_inputs(root: Path, inputs: list[dict], cfg: dict) -> dict:
    from .task_runner import _step_environment, file_hash
    from .task_guard import available
    from .actions import rscript_path
    from .temp_paths import cleanup
    available()
    if sys.platform != 'linux':
        from native_dist.doctor import require_probe
        require_probe(root)
    if any(Path(i['path']).suffix.lower() not in SUPPORTED for i in inputs):
        raise ValueError('Input inspection supports CSV, TSV, RDS and GMT; supply a supported representation')
    folder = root / '.runtime/input_inspections' / ('probe_' + uuid.uuid4().hex)
    folder.mkdir(parents=True, mode=0o700)
    # Retained local audit, including hashed join keys; never sent to the model.
    source = Path(__file__).with_name('input_probe.R')
    script = folder / 'probe.R'
    script.write_bytes(source.read_bytes())
    request = folder / 'request.json'
    request.write_text(json.dumps({'inputs': inputs}), encoding='utf-8')
    output = folder / 'inspection.json'
    env = _step_environment(root, folder, cfg, inputs)
    argv = [sys.executable, str(Path(__file__).with_name('task_guard.py')), str(folder), '--',
            rscript_path(root), '--vanilla', str(script), str(request), str(output)]
    try:
        proc = _run_guarded(root, folder, argv, env)
        log_path = folder / 'probe.log'
        log_path.write_text(proc.stdout + proc.stderr, encoding='utf-8')
        if proc.returncode or not output.is_file():
            # CI 的临时目录随运行销毁；把脱敏后的末尾带进错误信息，用户和日志都能看到原因。
            detail = ''
            try:
                from .model_config import redact
                detail = redact(log_path.read_text(encoding='utf-8', errors='replace')[-800:]).strip()
            except OSError:
                pass
            message = f'Input structure inspection failed; inspect local log {log_path}'
            if detail:
                message += '\n' + detail
            raise ValueError(message)
    finally:
        cleanup(root, folder.name, task_dir=folder)
    if any(file_hash(root / i['path']) != i['sha256'] for i in inputs):
        raise ValueError('Input changed during inspection')
    local = json.loads(output.read_text(encoding='utf-8'))
    for profile in local['profiles']:
        profile['columns'] = profile.get('columns') or {}
    local['version'] = VERSION
    stable = root / '.runtime/input_inspections' / (hashlib.sha256(output.read_bytes()).hexdigest() + '.json')
    if not stable.exists():
        with stable.open('x', encoding='utf-8') as handle: handle.write(output.read_text(encoding='utf-8'))
    local['audit_path'] = str(stable.relative_to(root))
    return local


def _list(value):
    return value if isinstance(value, list) else ([] if value is None else [value])


def public_inspection(local: dict) -> dict:
    """Allowlist aggregate structure; exclude keys, IDs and clinical values."""
    profiles = []
    for p in local['profiles']:
        q = {k: p[k] for k in ('path','sha256','object_class','dimensions','numeric_matrix','matrix_like',
                              'finite','negative_count','zero_rows','constant_rows','gene_sets') if k in p}
        q['columns'] = {name: {k: c[k] for k in ('type','missing','unique','known_levels')} for name,c in (p.get('columns') or {}).items()}
        q['axis_keys'] = {k:{f:v[f] for f in ('missing','duplicates')} for k,v in p['keys'].items() if k in {'rows','columns'}}
        profiles.append(q)
    joins = []
    for a in local['profiles']:
        if not a.get('numeric_matrix'): continue
        for b in local['profiles']:
            if a['path']==b['path']: continue
            for axis in ('rows','columns'):
                if axis not in a['keys']: continue
                left = set(_list(a['keys'][axis]['values']))
                for name, right in b['keys'].items():
                    if not name.startswith('column:'): continue
                    overlap = len(left & set(_list(right['values'])))
                    if overlap:
                        joins.append({'matrix':a['path'],'axis':axis,'metadata':b['path'],'key':name[7:],
                                      'matched':overlap,'matrix_unique':len(left),'metadata_unique':len(set(_list(right['values'])))})
    return {'version':VERSION,'profiles':profiles,'joins':joins,'software':local['software'],'apis':local['apis']}


def validate_bindings(bindings: dict, local: dict, request: str) -> dict:
    if not isinstance(bindings, dict): raise ValueError('input_bindings must be an object')
    ps = {p['path']:p for p in local['profiles']}
    matrices = bindings.get('matrices', [])
    if not isinstance(matrices,list): raise ValueError('input_bindings.matrices must be an array')
    if not matrices and bindings != {'inventory': True}:
        raise ValueError('Provide matrix bindings or explicit inventory-only bindings; clarify unsupported roles')
    for b in matrices:
        required={'path','sample_axis','metadata_path','sample_key','group_key','group_map','pair_key','preprocessing'}
        if not isinstance(b,dict) or not required <= b.keys(): raise ValueError('Incomplete matrix input binding')
        p, m = ps.get(b['path']), ps.get(b['metadata_path'])
        if not p or not p.get('numeric_matrix') or not m: raise ValueError('Binding requires numeric expression and inspected metadata')
        axis = b['sample_axis']
        if axis not in {'rows','columns'}: raise ValueError('sample_axis must be rows or columns')
        keys = p['keys'][axis]; mk = m['keys'].get('column:'+b['sample_key'])
        if not mk or keys['duplicates'] or mk['duplicates'] or keys['missing'] or mk['missing']:
            raise ValueError('Sample keys must be nonmissing and unique')
        if set(_list(keys['values'])) != set(_list(mk['values'])):
            raise ValueError('Expression and metadata sample keys must match exactly; explicitly resolve exclusions before approval')
        c = m['columns'].get(b['group_key']); mapping=b['group_map']
        if not c or c['missing'] or not isinstance(mapping,dict) or len(set(mapping.values()))<2:
            raise ValueError('Grouping requires a complete field and at least two explicit mapped groups')
        hashed = {hashlib.sha256(str(k).encode()).hexdigest() for k in mapping}
        if hashed != set(_list(c['level_hashes'])): raise ValueError('Group mapping must exactly cover all observed levels')
        if not all(isinstance(v,str) and v.strip() for v in mapping.values()): raise ValueError('Invalid group labels')
        # Tissue abbreviations require an explicit source; exact full words have fixed semantics.
        if set(mapping.values()) == {'tumor','normal'}:
            vocabulary={'tumor':'tumor','tumour':'tumor','normal':'normal','non_tumor':'normal',
                        'non-tumor':'normal','adjacent':'normal','control':'normal'}
            for label, target in mapping.items():
                canonical=vocabulary.get(label.lower())
                explicit=re.search(r'(?<![A-Za-z0-9_])'+re.escape(label)+r'\s*(?:表示|为|means|=|→|->)\s*'+re.escape(target)+r'(?![A-Za-z])',request,re.I)
                if canonical is not None and canonical != target:
                    raise ValueError('Tissue mapping reverses an explicit tissue label')
                if canonical is None and not explicit:
                    raise ValueError('Abbreviated tissue labels require an explicit user mapping; ask for clarification')
        pair = b['pair_key']
        if pair is not None:
            pc=m['columns'].get(pair)
            if not pc or pc['missing']: raise ValueError('Pair field is missing or incomplete')
            pair_values=_list(m['keys']['column:'+pair]['values']); group_values=_list(m['keys']['column:'+b['group_key']]['values'])
            seen={}
            for pair_id,g in zip(pair_values,group_values): seen.setdefault(pair_id,[]).append(g)
            if any(len(v)!=len(mapping) or set(v)!=hashed for v in seen.values()):
                raise ValueError('Each pair must have exactly one sample from every group')
        if b['preprocessing'] not in {'none','log2p1'}: raise ValueError('Choose explicit preprocessing none or log2p1')
        if b['preprocessing']=='log2p1' and (not re.search(r'log2\s*\(\s*x\s*\+\s*1\s*\)|log2p1',request,re.I) or re.search(r'(?:不做|不进行|不要|without|no)\s*log2',request,re.I)):
            raise ValueError('log2 preprocessing requires explicit user instruction; ask for clarification')
        if b['preprocessing']=='log2p1' and p.get('negative_count',0): raise ValueError('log2p1 cannot be applied to negative values')
        if not p.get('finite',False): raise ValueError('Nonfinite expression requires an explicit missing-value policy')
        if p['keys']['rows' if axis=='columns' else 'columns']['duplicates']:
            raise ValueError('Duplicate feature names require an explicit aggregation policy')
        for path in bindings.get('gene_sets',[]):
            gs=ps.get(path)
            if not gs or 'gene_sets' not in gs: raise ValueError('Gene set resource must be an inspected named character list or GMT')
            features=set(_list(p['keys']['rows' if axis=='columns' else 'columns']['values']))
            if any(not features.intersection(_list(s)) for s in gs['set_members']):
                raise ValueError('A gene set has zero coverage; clarify feature identifiers before scoring')
    return bindings

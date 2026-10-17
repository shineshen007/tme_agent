"""Offline v7 pipeline acceptance on new synthetic files, never study data."""
from __future__ import annotations
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import subprocess
import uuid


def acceptance(root):
    from multiomics_tui.llm import load_tui_config
    from multiomics_tui.input_inspection import inspect_inputs, public_inspection
    from multiomics_tui.task_planner import _input, validate_plan
    from multiomics_tui.task_sessions import TaskStore
    from multiomics_tui.task_runner import run
    from multiomics_tui.actions import rscript_path
    from native_dist.doctor import require_probe
    require_probe(root)
    folder = root/'analysis'/('v7_fixture_' + uuid.uuid4().hex)
    folder.mkdir(parents=True)
    (folder/'input.csv').write_text('label,value\na,2\nb,3\n',encoding='utf-8')
    subprocess.run([rscript_path(root),'--vanilla','-e',
        'saveRDS(data.frame(label=c("a","b"),value=c(2,3)),commandArgs(TRUE)[1])',str(folder/'input.rds')],check=True,capture_output=True)
    paths=[(folder/name).relative_to(root).as_posix() for name in ['input.csv','input.rds']]
    cfg=load_tui_config(root)
    inspected=inspect_inputs(root,[_input(root,p) for p in paths],cfg)
    request='Offline synthetic file inventory: '+', '.join(paths)
    raw={'goal':request,'analysis_unit':'synthetic file','method':'descriptive inventory','parameters':[],
         'inputs':paths,'input_bindings':{'inventory':True},
         'steps':[{'id':'csv_copy','language':'python','purpose':'Read synthetic CSV','outputs':['tables/copy.csv']},
                  {'id':'rds_check','language':'r','purpose':'Check synthetic RDS and previous CSV',
                   'outputs':['objects/copy.rds','tables/verified.csv']}],
         'limitations':['Offline deterministic model; no biological inference or real API validation']}
    store=TaskStore(root); state=store.create(request)
    plan=validate_plan(root,raw,request,cfg,inspection=inspected)
    state=store.save_plan(state['task_id'],plan);store.approve(state['task_id'],state['plan_hash'])
    pycode='import os,csv\nfrom pathlib import Path\np=Path(os.environ["PROJECT_ROOT"])/'+repr(paths[0])+'\nwith p.open(newline="") as f: rows=list(csv.DictReader(f))\nout=Path(os.environ["TASK_DIR"])/"tables/copy.csv"\nwith out.open("w",newline="") as f:\n w=csv.DictWriter(f,fieldnames=["label","value"]);w.writeheader();w.writerows(rows)\n'
    rcode='x<-readRDS(file.path(Sys.getenv("PROJECT_ROOT"),'+json.dumps(paths[1])+')); y<-read.csv(file.path(Sys.getenv("TASK_DIR"),"tables/copy.csv")); stopifnot(identical(x$label,y$label),all(x$value==y$value)); saveRDS(x,file.path(Sys.getenv("TASK_DIR"),"objects/copy.rds")); write.csv(x,file.path(Sys.getenv("TASK_DIR"),"tables/verified.csv"),row.names=FALSE)\n'
    class Model:
        def chat_json(self,role,system,user,schema_name,validator=None):
            if role!='tui_task_codegen': raise ValueError('Unexpected mock role')
            step=json.loads(user)['step']; sid=step['id']
            contracts=[{'path':p,'structure':'synthetic table with label and value','format':'rds_table' if p.endswith('.rds') else 'csv',
                        'columns':['label','value']} for p in step['outputs']]
            parsed={'files':[{'step_id':sid,'content':pycode if sid=='csv_copy' else rcode}], 'output_contracts':contracts}
            if validator: validator(parsed)
            return {'parsed':parsed}
    if run(root,state['task_id'],client=Model()) != 0:
        raise RuntimeError('Synthetic task failed: '+state['task_id'])
    task=root/'analysis'/state['task_id']
    with (task/'tables/verified.csv').open(newline='') as f:
        rows=list(csv.DictReader(f))
    if rows != [{'label':'a','value':'2'},{'label':'b','value':'3'}]:
        raise RuntimeError('Synthetic output differs')
    for item in plan['inputs']:
        if hashlib.sha256((root/item['path']).read_bytes()).hexdigest()!=item['sha256']:
            raise RuntimeError('Synthetic input changed')
    manifest=json.loads((task/'code_manifest.json').read_text(encoding='utf-8'))
    if manifest['status']!='succeeded' or store.get(state['task_id'])['status']!='succeeded':
        raise RuntimeError('Task completion record mismatch')
    return {'status':'passed','task_id':state['task_id'],'input_files':2,'outputs':3,'table_rows':2,
            'input_inspection':True,'python_and_r':True,'rds_contract':True,
            'input_hashes_unchanged':True,'real_api_tested':False}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--project-root',type=Path,required=True)
    root=parser.parse_args().project_root.resolve()
    record={'status':'failed'}
    try:
        record=acceptance(root)
    except Exception as exc:
        from multiomics_tui.model_config import redact
        record.update(error_type=type(exc).__name__,error=redact(str(exc)))
    (root/'.runtime').mkdir(exist_ok=True)
    (root/'.runtime/v7_acceptance.json').write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(record,ensure_ascii=False,indent=2))
    return 0 if record['status']=='passed' else 1


if __name__=='__main__':
    raise SystemExit(main())

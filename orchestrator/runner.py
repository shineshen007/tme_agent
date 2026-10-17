"""CLI and resumable run lifecycle with source and artifact verification."""
from __future__ import annotations
import argparse
import logging
import re
import os
import fcntl
from pathlib import Path
from .io import load_config, provenance, write_json, read_json, artifact, now
from .inputs import prepare_inputs
from .checkers import validate_schema, verify_artifacts, validate_de
from .report import render_report


def acquire_run_lock(run):
    lease=(Path(run)/'.run.lock').open('a+')
    try:
        fcntl.flock(lease.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        lease.close()
        raise ValueError('Another process is already running this run-id')
    lease.seek(0);lease.truncate();lease.write(str(os.getpid())+'\n');lease.flush()
    return lease


def verify_run(root,run):
    root,run=Path(root),Path(run)
    saved=read_json(run/'run.json')
    current=provenance(root,saved['config'])
    if saved['fingerprint']!=current['fingerprint']:
        raise ValueError('Source/config/environment drift; start a new run')
    validation=read_json(run/'validation/validate_report.json')
    validate_schema(validation,root/'schemas/input_manifest.schema.json')
    verify_artifacts(run,validation['artifacts'])
    if not (run/'artifact_manifest.json').exists():
        raise ValueError('Run has no finalized artifact manifest; execution is not yet verified complete')
    verify_artifacts(run,read_json(run/'artifact_manifest.json')['artifacts'])
    for p in run.glob('*/*/de.csv'):
        validate_de(p)
    return {'status':'verified','fingerprint':saved['fingerprint']}


def publish_run_dossiers(root, run, cfg, graph_result):
    """Publish built target dossiers to the project-root result directory after a successful run.

    A completed run supersedes the packages published by earlier runs (the previous package is
    backed up to ``result/.replaced-<utc>-<slug>/`` before replacement) unless
    ``deliverables.dossiers.overwrite`` is set to ``refuse``. Publication is degraded visibly,
    never silently: a failure marks the run partial and is recorded in evidence_status.
    """
    block=((cfg.get('deliverables') or {}).get('dossiers') or {})
    if (graph_result.get('agent_result') or {}).get('status') == 'blocked':
        return {'status':'skipped_blocked_reasoning'}
    if block.get('enabled',True) is False:
        return {'status':'disabled'}
    if block.get('publish',True) is False:
        return {'status':'skipped_no_build'}
    if (graph_result.get('dossier_result') or {}).get('status') in {None,'failed','skipped_no_targets','disabled'}:
        return {'status':'skipped_no_build'}
    from .dossiers import publish_dossiers
    try:
        return publish_dossiers(root,run,cfg,force=str(block.get('overwrite') or 'replace')!='refuse')
    except Exception as exc:
        logging.exception('Dossier publication failed')
        return {'status':'failed','error':str(exc),'n_published':0,'n_unchanged':0,'n_failed':1}


def main(argv=None):
    parser=argparse.ArgumentParser(description='Paired multi-omics deterministic workflow')
    parser.add_argument('command',choices=['run','resume','verify','dossiers','verify-dossiers'])
    parser.add_argument('--project-root',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--config',type=Path)
    parser.add_argument('--run-id',required=True)
    parser.add_argument('--stage',choices=['data','all'],default='all')
    parser.add_argument('--force',action='store_true',help='Replace conflicting published dossiers')
    parser.add_argument('--no-publish',action='store_true',help='Build target dossiers without publishing')
    parser.add_argument('--no-rerender',action='store_true',help='Skip the byte-identical re-render check')
    args=parser.parse_args(argv);root=args.project_root.resolve()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,100}',args.run_id):
        parser.error('Invalid run-id')
    run=root/'workspace/runs'/args.run_id
    if args.command=='verify-dossiers':
        from .dossiers import verify_dossiers
        print(verify_dossiers(root,run,load_config(root,args.config),rerender=not args.no_rerender));return 0
    if args.command=='dossiers':
        # Read-only derivation from a completed run: source integrity is checked against the run's
        # frozen artifact manifest. Code-fingerprint matching is intentionally not required here
        # (the generator itself is recorded, by hash, inside each dossier manifest).
        manifest=run/'artifact_manifest.json'
        if not manifest.exists():
            raise ValueError('Run has no finalized artifact manifest; dossier inputs cannot be validated')
        verify_artifacts(run,read_json(manifest)['artifacts'])
        from .dossiers import build_target_dossiers,publish_dossiers
        cfg=load_config(root,args.config)
        block=((cfg.get('deliverables') or {}).get('dossiers') or {})
        build=build_target_dossiers(root,run,cfg)
        if args.no_publish:
            print(build);return 0
        publish=publish_dossiers(root,run,cfg,force=args.force or str(block.get('overwrite') or 'replace')!='refuse')
        print({'build':{k:build[k] for k in ['status','n_targets','errors']},'publish':publish})
        return 0
    if args.command=='verify':
        saved=read_json(run/'run.json')
        if saved['fingerprint'] != provenance(root,load_config(root,args.config))['fingerprint']:
            raise ValueError('Verification rejected: current config/source/environment differs from frozen run')
        print(verify_run(root,run));return 0
    cfg=load_config(root,args.config)
    if args.command in {'run','resume'} and args.stage=='all':
        # Fail before hours of computation if the LLM layer is enabled but unusable.
        from .llm_client import llm_preflight
        llm_preflight(cfg)
    if args.command=='run':
        snapshot=provenance(root,cfg)
        run.mkdir(parents=True,exist_ok=False)
        write_json(run/'run.json',snapshot)
    else:
        snapshot=read_json(run/'run.json')
        if snapshot['fingerprint']!=provenance(root,cfg)['fingerprint']:
            raise ValueError('Resume rejected: source/config/environment changed; start a new run')
    lease=acquire_run_lock(run)
    write_json(run/'process.json',{'pid':os.getpid(),'parent_pid':os.getppid(),'started_at':now(),'run_id':args.run_id})
    (run/'logs').mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s',
                        handlers=[logging.FileHandler(run/'logs/orchestrator.log'),logging.StreamHandler()],force=True)
    write_json(run/'run_state.json',{'status':'running','stage':args.stage,'updated_at':now()})
    try:
        manifest=run/'validation/validate_report.json'
        if manifest.exists():
            validation=read_json(manifest);verify_artifacts(run,validation['artifacts'])
        else:
            # Preserve an interrupted preparation attempt, then rebuild from immutable raw inputs.
            if (run/'inputs').exists():
                archived=run/('inputs.incomplete.'+now().replace(':','-'))
                (run/'inputs').rename(archived)
            validation=prepare_inputs(root,run,cfg)
        validate_schema(validation,root/'schemas/input_manifest.schema.json')
        if args.stage=='data':
            state={'technical_status':'data_validated','scientific_status':'not_analyzed'}
        else:
            from .graph import build_graph
            graph=build_graph(root,run,cfg)
            result=graph.invoke({'run_id':args.run_id},{'max_concurrency':cfg['runtime']['workers'],
                                                    'recursion_limit':max(32,16*cfg.get('reasoning',{}).get('max_iterations',2)+16)})
            from .figures import render_figures
            render_figures(root,run)
            result['dossier_publish']=publish_run_dossiers(root,run,cfg,result)
            state=render_report(run,result)
        # Verify raw inputs and frozen code again after computation, not just at startup.
        if provenance(root,cfg)['fingerprint']!=snapshot['fingerprint']:
            raise ValueError('Inputs or code changed during execution')
        write_json(run/'run_state.json',{**state,'updated_at':now()})
        logging.info('Run finished: %s',state)
        logging.shutdown()
        artifacts=[artifact(run,p) for p in sorted(run.rglob('*')) if p.is_file() and p.name!='artifact_manifest.json']
        write_json(run/'artifact_manifest.json',{'artifacts':artifacts})
        print(f'Output: {run}')
        return 0 if state['technical_status'] in {'complete','data_validated'} else 2
    except Exception as exc:
        logging.exception('Run stopped')
        write_json(run/'run_state.json',{'status':'failed','error':str(exc),'updated_at':now()})
        return 2
    finally:
        lease.close()

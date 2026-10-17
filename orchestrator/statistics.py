"""Whitelisted R tool dispatch and numeric QC. No model-generated commands."""
from __future__ import annotations
import os
import subprocess
import logging
from pathlib import Path
import numpy as np
import pandas as pd
from scipy import stats
from .io import artifact, read_json, write_json, sha256
from .checkers import verify_artifacts

LOG = logging.getLogger(__name__)


def r_environment(cfg):
    env = os.environ.copy()
    env.update({'OPENBLAS_NUM_THREADS': '1', 'OMP_NUM_THREADS': '1', 'MKL_NUM_THREADS': '1',
                'LANGCHAIN_TRACING_V2': 'false', 'LANGSMITH_TRACING': 'false'})
    if cfg['runtime']['r_library_paths']:
        env['R_LIBS_USER'] = os.pathsep.join(cfg['runtime']['r_library_paths'])
    return env


def invoke_r(root, run, cfg, job, script):
    root, run = Path(root), Path(run)
    out = Path(job['output_dir'])
    out.mkdir(parents=True, exist_ok=True)
    job_path = out / ('job.json' if script == '02_statistics.R' else f'{script}.job.json')
    if job_path.exists() and read_json(job_path) != job:
        raise ValueError(f'Job changed: {job_path}; start a new run')
    completed = out / (script + '.completed.json')
    if completed.exists():
        cached = read_json(completed)
        if cached['job'] != job:
            raise ValueError(f'Cached job changed: {completed}')
        verify_artifacts(run, cached['artifacts'])
        return
    write_json(job_path, job)
    log = out / (script + '.log')
    command = [cfg['runtime']['rscript'], '--vanilla', str(root / 'scripts' / script), str(job_path)]
    write_json(out / (script + '.command.json'), {'argv': command, 'job_sha256': sha256(job_path)})
    LOG.info('Running %s %s %s', script, job.get('omics'), job.get('cohort_id'))
    # Deterministic statistical errors are not retried. Transient infrastructure errors only.
    for attempt in range(cfg['runtime']['retries']):
        with log.open('a') as stream:
            stream.write(f'\nATTEMPT {attempt + 1}\n'); stream.flush()
            result = subprocess.run(command, env=r_environment(cfg), stdout=stream, stderr=subprocess.STDOUT, cwd=root)
        if result.returncode == 0:
            write_json(completed, {'job': job, 'artifacts': [artifact(run,p) for p in sorted(out.rglob('*')) if p.is_file() and p != completed]})
            return
        tail = log.read_text(errors='replace')[-5000:]
        transient = any(t in tail.lower() for t in ['connection reset by peer', 'temporary failure in name resolution'])
        if not transient:
            break
    raise RuntimeError(f'{script} exited {result.returncode}; see {log.relative_to(run)}\n{tail[-1800:]}')


def qc_metabolomics(run, cfg):
    run = Path(run)
    x = pd.read_csv(run / 'inputs/metabolomics_with_qc.csv', index_col=0)
    md = pd.read_csv(run / 'inputs/injection_metadata.csv').set_index('sample.name')
    qc_names = md.index[md['class'] == 'QC']
    q = x.loc[:, qc_names]
    summary = pd.DataFrame({'feature_id': x.index, 'qc_rsd_percent': 100*q.std(axis=1,ddof=1)/q.mean(axis=1)})
    summary['flag_rsd_over_cutoff'] = summary.qc_rsd_percent > cfg['analysis']['metabolomics']['qc_rsd_cutoff']
    pieces = []
    for batch in sorted(md.batch.unique()):
        names = md.index[(md['class']=='QC') & (md.batch==batch)]
        for feature, values in x.loc[:, names].iterrows():
            corr = stats.spearmanr(md.loc[names,'injection.order'],values)
            pieces.append({'feature_id': feature, 'batch': str(batch), 'n_qc': len(names),
                           'rsd_percent': 100*values.std(ddof=1)/values.mean(),
                           'order_spearman': corr.statistic, 'pvalue': corr.pvalue})
    drift = pd.DataFrame(pieces)
    drift['padj'] = stats.false_discovery_control(drift.pvalue.fillna(1).to_numpy(), method='bh')
    out = run / 'metabolomics/qc'
    out.mkdir(parents=True,exist_ok=True)
    summary.to_csv(out/'qc_rsd.csv',index=False)
    drift.to_csv(out/'qc_drift_by_batch.csv',index=False)
    # No automatic drift correction: the supplied matrix may already be corrected.
    excluded = summary.loc[summary.flag_rsd_over_cutoff,'feature_id']
    original = pd.read_csv(run/'inputs/metabolomics.csv',index_col=0)
    original.loc[~original.index.isin(excluded)].to_csv(out/'analysis_matrix.csv',index_label='feature_id')
    write_json(out/'qc_status.json',{'n_features': len(summary),'n_flagged': len(excluded),
               'normalization': 'none', 'history': 'unknown', 'interpretation': 'QC of supplied matrix only; not evidence that raw assay QC passed'})
    return out/'analysis_matrix.csv'


def analyze_domain(root, run, cfg, omics):
    root, run = Path(root), Path(run)
    result_path = run / omics / 'result.json'
    if result_path.exists():
        previous = read_json(result_path)
        if previous['status'] in {'complete','review_only'}:
            for a in previous['artifacts']:
                if sha256(run/a['path']) != a['sha256']:
                    raise ValueError(f'Cached artifact changed: {a["path"]}')
            return previous
    (run/omics).mkdir(parents=True,exist_ok=True)
    cohorts = ['full159','triple109'] if omics != 'metabolomics' else ['triple109','same_batch108']
    matrix = run / 'inputs' / f'{omics}.csv'
    errors = []
    summaries = []
    try:
        if omics == 'metabolomics':
            matrix = qc_metabolomics(run,cfg)
        for cohort in cohorts:
            out = run/omics/cohort
            key = {'transcriptomics':'rna_log2fc','proteomics':'prot_log2fc','metabolomics':'met_log2fc'}[omics]
            job = {'omics':omics,'cohort_id':cohort,'matrix':str(matrix),'metadata':str(run/'inputs/sample_registry.csv'),
                   'output_dir':str(out),'seed':cfg['study']['seed'],'min_pairs':cfg['study']['min_pairs'],
                   'alpha':cfg['analysis']['de']['padj'],'effect_cutoff':cfg['analysis']['de'][key],
                   'min_total_count':cfg['analysis']['rna']['min_total_count'],'fit_type':cfg['analysis']['rna']['fit_type'],
                   'max_nonconverged_fraction':cfg['analysis']['rna'].get('max_nonconverged_fraction',0.05),
                   'r_workers':cfg['runtime'].get('r_workers',1),'r_chunk_size':cfg['runtime'].get('r_chunk_size',500),
                   'checkpoint_script':str(root/'scripts/02a_deseq_checkpointed.R'),
                   'r_library_paths':cfg['runtime']['r_library_paths']}
            invoke_r(root,run,cfg,job,'02_statistics.R')
            summaries.append(read_json(out/'statistics.json'))
    except Exception as exc:
        LOG.exception('Domain failed: %s',omics)
        errors.append({'step':omics,'message':str(exc),'resolved':False})
    paths = sorted(p for p in (run/omics).rglob('*') if p.is_file() and p != result_path)
    result = {'schema_version':'1.0','status':'failed' if errors else 'review_only','omics':omics,
              'cohort_id':cohorts,'contrast':'tumor_vs_adjacent','n_pairs':{s['cohort_id']:s['n_pairs'] for s in summaries},
              'de':summaries,'artifacts':[artifact(run,p) for p in paths],
              'qc':['observational_paired_tissue_design','source_processing_limits_recorded'], 'errors':errors}
    write_json(result_path,result)
    return result

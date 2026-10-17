"""Frozen pathway resources and expression-program interpretation."""
from pathlib import Path
import pandas as pd
from scipy.stats import hypergeom, false_discovery_control
import yaml
from .io import write_json
from .statistics import invoke_r


def run_pathways(root, run, cfg, domain_results):
    root, run = Path(root), Path(run)
    status = {}
    axes = yaml.safe_load((root/'knowledge/tme_metabolism_axes.yaml').read_text())['axes']
    genes = sorted({g for a in axes for g in a['genes']})
    for omics in ['transcriptomics','proteomics']:
        for cohort in ['full159','triple109']:
            key = f'{omics}/{cohort}'
            if domain_results[omics]['status'] not in {'complete','review_only'}:
                status[key] = {'status':'blocked','reason':'upstream_statistics_failed'};continue
            target = run/'pathways'/omics/cohort
            job = {'omics':omics,'cohort_id':cohort,'statistics_dir':str(run/omics/cohort),
                   'output_dir':str(target),'mapping':str(root/'knowledge/resources/gene_mapping.csv'),
                   'gmt':str(root/cfg['analysis']['pathway']['gmt']),
                   'immune_gmt':str(root/cfg['analysis']['pathway']['immune_gmt']),
                   'min_size':cfg['analysis']['pathway']['min_size'],'max_size':cfg['analysis']['pathway']['max_size'],
                   'axis_genes':genes,'seed':cfg['study']['seed']}
            try:
                invoke_r(root,run,cfg,job,'03_pathways.R')
                status[key] = {'status':'complete'}
            except Exception as e:
                status[key] = {'status':'failed','reason':str(e)}
    met_links = root/'knowledge/resources/kegg_compound_pathway.tsv'
    if met_links.exists() and domain_results['metabolomics']['status'] in {'complete','review_only'}:
        ann = pd.read_csv(run/'inputs/metabolite_annotations.csv')
        de = pd.read_csv(run/'metabolomics/triple109/de.csv')
        # Primary compound ORA is deliberately limited to non-ambiguous initial MS/MS seeds.
        a = ann.loc[(ann.candidate_count==1)&(ann.annotation_type=='seed')]
        a = a.merge(de[['feature_id','selected']],on='feature_id',validate='many_to_one')
        universe = set(a.compound_id);hits=set(a.loc[a.selected,'compound_id'])
        links = pd.read_csv(met_links,sep='\t',names=['pathway','compound'])
        links['compound'] = links.compound.str.replace('cpd:','',regex=False)
        links['pathway'] = links.pathway.str.replace('path:','',regex=False)
        human=pd.read_csv(root/'knowledge/resources/kegg_human_pathways.tsv',sep='\t',names=['human_id','pathway_name'])
        human['pathway']=human.human_id.str.replace('hsa','map',regex=False)
        links=links.merge(human[['pathway','pathway_name']],on='pathway',validate='many_to_one')
        rows=[]
        for path,g in links.groupby('pathway'):
            members=set(g.compound)&universe;k=len(members&hits)
            eligible=len(members)>=3
            rows.append({'pathway':path,'pathway_name':g.pathway_name.iloc[0],'background_hits':len(members),'selected_hits':k,'universe_size':len(universe),
                         'selected_total':len(hits),'pvalue':hypergeom.sf(k-1,len(universe),len(members),len(hits)) if eligible else float('nan'),
                         'status':'tested' if eligible else 'insufficient_measured_members','members':';'.join(sorted(members)),
                         'interpretation':'putative_unambiguous_initial_seed_compounds_only'})
        result=pd.DataFrame(rows);ok=result.pvalue.notna();result.loc[ok,'padj']=false_discovery_control(result.loc[ok,'pvalue'].to_numpy())
        target=run/'pathways/metabolomics';target.mkdir(parents=True,exist_ok=True)
        result.to_csv(target/'kegg_ora_full.csv',index=False)
        a.to_csv(target/'kegg_ora_background.csv',index=False)
        status['metabolomics/triple109']={'status':'complete','identity':'putative_not_MSI_verified'}
    else:
        status['metabolomics/triple109']={'status':'blocked','reason':'KEGG_resource_or_metabolomics_unavailable'}
    write_json(run/'pathways/status.json',status)
    return status

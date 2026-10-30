"""Cross-omics evidence integration; associations never imply flux or causation."""
from __future__ import annotations
import hashlib
from pathlib import Path
import numpy as np
import pandas as pd
from scipy import stats
import yaml
from .io import write_json, read_json
from .checkers import validate_hypotheses
from .evidence_records import write_evidence_registry_v2
from .evidence_graph import build_evidence_graph


def evidence_id(omics, cohort, feature):
    return 'ev_' + hashlib.sha256(f'{omics}|{cohort}|{feature}|tumor_vs_adjacent'.encode()).hexdigest()[:20]


def layer_evidence_id(layer, *parts):
    """Stable ids for the pre-registered candidate-axis layers; same ev_ namespace as DE."""
    return 'ev_' + hashlib.sha256('|'.join([layer, *[str(p) for p in parts]]).encode()).hexdigest()[:20]


def candidate_axis_evidence(run, cfg, axes):
    """Prognosis and T-cell correlation evidence for the pre-registered candidate axis.

    Frozen thresholds, complete reporting: every tested feature is registered, including
    not-estimable Cox models and non-significant correlations. Deconvolution proportions and
    RNA signature scores are labelled estimate_association and never count as measured omics.
    """
    run, cfg = Path(run), cfg
    pcfg, tcfg = cfg['analysis']['prognosis'], cfg['analysis']['tcell']
    axis = next(a for a in axes if a['axis'] == pcfg['axis'])
    alpha = float(pcfg['fdr'])
    prog_rows, tcell_rows = [], []
    cox_path = run / 'prognosis/cox_models.csv'
    if cox_path.exists():
        cox = pd.read_csv(cox_path)
        # The R tool already reports the predeclared family and the family-wise BH padj.
        # Reuse those values verbatim; only legacy outputs without them are adjusted here.
        if 'family' not in cox.columns or 'padj' not in cox.columns:
            cox['family'] = ['prognosis_' + '_'.join([str(c), str(o), str(m)])
                             for c, o, m in zip(cox.cohort, cox.outcome, cox.model_type)]
            cox = adjust(cox, 'family')
        else:
            cox['family'] = cox['family'].astype(str)
            cox['padj'] = pd.to_numeric(cox['padj'], errors='coerce')
            tested = cox.status.eq('tested')
            if tested.any() and not np.isfinite(cox.loc[tested, 'padj']).all():
                raise ValueError('Prognosis output lacks finite family-wise padj for tested models')
        cox['selected'] = cox.status.eq('tested') & cox.padj.notna() & cox.padj.le(alpha)
        cox['cohort_id'] = cox.cohort.astype(str)
        cox['direction'] = np.where(cox.hr.gt(1), 'up', np.where(cox.hr.lt(1), 'down', ''))
        cox.loc[cox.status.ne('tested'), ['direction']] = ''
        cox['evidence_layer'] = 'prognosis'
        cox['claim_type'] = 'prognostic_association'
        cox['evidence_id'] = [layer_evidence_id('prognosis', r.cohort, r.feature_id, r.outcome, r.model_type)
                              for r in cox.itertuples()]
        prog_rows = cox
        prog_rows.to_csv(run / 'integration/prognosis_association.csv', index=False)
    feat_path = run / 'prognosis/patient_features.csv'
    clinical_path = run / 'inputs/clinical_analysis.csv'
    if feat_path.exists():
        feat = pd.read_csv(feat_path)
        clinical = pd.read_csv(clinical_path).set_index('patient_id')
        axis_features = feat.loc[feat.value_type == 'axis_feature']
        axis_features = axis_features.drop_duplicates(['omics', 'feature_id'])
        proportions = feat.loc[feat.value_type == 'tcell_proportion']
        signatures = feat.loc[feat.value_type == 'tcell_signature']
        readouts = []
        for panel, frame in proportions.groupby('panel'):
            celltypes = {ct: frame.loc[frame.entity == ct].set_index('patient_id')['value']
                         for ct in tcfg['deconvolution_celltypes'] if (frame.entity == ct).any()}
            for ct, series in celltypes.items():
                readouts.append({'kind': 'tcell_proportion', 'panel': str(panel), 'readout': ct,
                                 'limitation': 'external atlas NNLS estimate, not a measured fraction',
                                 'series': series})
            if all(ct in celltypes for ct in tcfg['deconvolution_celltypes']):
                total = sum(celltypes[ct] for ct in tcfg['deconvolution_celltypes'])
                readouts.append({'kind': 'tcell_proportion', 'panel': str(panel), 'readout': 'total_T',
                                 'limitation': 'sum of estimated CD4T and CD8T fractions; estimate, not measured',
                                 'series': total})
        for name, frame in signatures.groupby('entity'):
            readouts.append({'kind': 'tcell_signature', 'panel': 'rna', 'readout': str(name),
                             'limitation': 'RNA-derived ssGSEA score sharing the RNA source; not independent evidence',
                             'series': frame.set_index('patient_id')['value']})
        bootstrap = int(cfg['analysis']['integration']['bootstrap_replicates'])
        seed = int(cfg['study']['seed'])
        for row in axis_features.itertuples():
            x = feat.loc[(feat.value_type == 'axis_feature') & (feat.feature_id == row.feature_id) &
                         (feat.omics == row.omics)].set_index('patient_id')['value']
            for readout in readouts:
                pair = pd.DataFrame({'x': x, 'y': readout['series']}).dropna()
                if len(pair) < 4:
                    continue
                purity = clinical['tumor_purity'].reindex(pair.index)
                modes = [('unadjusted', None)]
                if tcfg.get('purity_sensitivity', True) and purity.notna().sum() >= 4:
                    modes.append(('purity_adjusted', purity.to_numpy()))
                for mode, covariate in modes:
                    base = {'axis': axis['axis'], 'entity': row.entity, 'omics': row.omics,
                            'feature_id': row.feature_id, 'axis_entity': row.entity,
                            'readout': readout['readout'], 'readout_kind': readout['kind'],
                            'readout_panel': readout['panel'], 'limitation': readout['limitation']}
                    if readout['kind'] == 'tcell_proportion':
                        family = f'axis_tcell_proportion_{mode}_{readout["panel"]}'
                        effect_scale = 'spearman_rho_tumor_value_vs_estimated_proportion'
                    else:
                        family = f'axis_tcell_signature_{mode}'
                        effect_scale = 'spearman_rho_tumor_value_vs_ssgsea_score'
                    cs = correlate(pair.x, pair.y, seed=seed,
                                   bootstrap=bootstrap if covariate is None else 0, covariate=covariate)
                    tcell_rows.append({'family': family, 'cohort_id': 'triple109', 'n_pairs': cs['n_pairs'],
                                       'rho': cs['rho'], 'effect': cs['rho'], 'pvalue': cs['pvalue'],
                                       'ci_low': cs['ci_low'], 'ci_high': cs['ci_high'],
                                       'ci_method': cs['ci_method'], 'status': cs['status'],
                                       'effect_scale': effect_scale, **base})
    tcell = pd.DataFrame(tcell_rows)
    if len(tcell):
        tcell['padj'] = np.nan
        for _, group in tcell.groupby('family', dropna=False):
            ok = group.pvalue.notna()
            if ok.any():
                tcell.loc[group.index[ok], 'padj'] = stats.false_discovery_control(group.loc[ok, 'pvalue'].to_numpy())
        tcell['selected'] = tcell.padj.le(float(tcfg['fdr']))
        tcell['direction'] = np.where(tcell.rho.gt(0), 'positive', np.where(tcell.rho.lt(0), 'negative', ''))
        tcell['evidence_layer'] = 'tcell_correlation'
        tcell['claim_type'] = 'estimate_association'
        tcell['evidence_id'] = [layer_evidence_id('tcell', r.readout_kind, r.readout_panel, r.feature_id,
                                                  r.readout, r.family) for r in tcell.itertuples()]
        tcell = tcell.drop_duplicates('evidence_id')
        tcell.to_csv(run / 'integration/tcell_correlation.csv', index=False)
    return prog_rows, tcell


def correlate(x, y, seed=2024, bootstrap=0, covariate=None):
    a,b=np.asarray(x,dtype=float),np.asarray(y,dtype=float)
    ok=np.isfinite(a)&np.isfinite(b)
    if covariate is not None:
        c=np.asarray(covariate,dtype=float);ok &= np.isfinite(c)
    a,b=a[ok],b[ok];n=len(a)
    if n<4 or np.ptp(a)==0 or np.ptp(b)==0:
        return {'n_pairs':n,'rho':np.nan,'pvalue':np.nan,'ci_low':np.nan,'ci_high':np.nan,'ci_method':'not_estimable','status':'not_estimable'}
    if covariate is None:
        r,p=stats.spearmanr(a,b)
    else:
        design=np.column_stack([np.ones(n),stats.rankdata(c[ok])])
        a,b=stats.rankdata(a),stats.rankdata(b)
        a=a-design@np.linalg.lstsq(design,a,rcond=None)[0]
        b=b-design@np.linalg.lstsq(design,b,rcond=None)[0]
        r=stats.pearsonr(a,b).statistic
        p=2*stats.t.sf(abs(r)*np.sqrt((n-3)/max(1-r*r,1e-15)),df=n-3)
    z=np.arctanh(np.clip(r,-1+1e-12,1-1e-12));w=1.96/np.sqrt(max(n-(4 if covariate is not None else 3),1))
    low,high=np.tanh([z-w,z+w]);method='approximate_Fisher_z_for_rank_correlation'
    if bootstrap and covariate is None:
        rng=np.random.default_rng(seed);idx=rng.integers(0,n,size=(bootstrap,n))
        aa=stats.rankdata(a[idx],axis=1);bb=stats.rankdata(b[idx],axis=1)
        aa-=aa.mean(axis=1,keepdims=True);bb-=bb.mean(axis=1,keepdims=True)
        denom=np.sqrt((aa*aa).sum(axis=1)*(bb*bb).sum(axis=1))
        rr=np.divide((aa*bb).sum(axis=1),denom,out=np.full(bootstrap,np.nan),where=denom>0)
        if np.isfinite(rr).sum()>=.9*bootstrap:
            low,high=np.nanquantile(rr,[.025,.975]);method=f'patient_bootstrap_percentile_{bootstrap}'
    return {'n_pairs':n,'rho':float(r),'pvalue':float(p),'ci_low':float(low),'ci_high':float(high),'ci_method':method,'status':'tested'}


def adjust(frame, groups):
    frame['padj']=np.nan
    for _,g in frame.groupby(groups,dropna=False):
        ok=g.pvalue.notna()
        if ok.any():
            frame.loc[g.index[ok],'padj']=stats.false_discovery_control(g.loc[ok,'pvalue'].to_numpy())
    return frame


def integrate(root, run, cfg, domain_results, scrna_result=None, prognosis_result=None):
    root,run=Path(root),Path(run)
    out=run/'integration';out.mkdir(parents=True,exist_ok=True)
    axes=yaml.safe_load((root/'knowledge/tme_metabolism_axes.yaml').read_text())['axes']
    scrna_ok=scrna_result is not None and scrna_result.get('status') in {'complete','review_only'}
    prognosis_ok=prognosis_result is not None and prognosis_result.get('status') in {'complete','review_only'}
    literature_path=root/'knowledge/literature/pubmed_candidates.json'
    literature=read_json(literature_path) if literature_path.exists() else {'records':[],'status':'not_retrieved'}
    write_json(run/'report/literature_candidates.json',literature)
    ann=pd.read_csv(run/'inputs/metabolite_annotations.csv')
    registry=[];tables={};deltas={}
    for omics,result in domain_results.items():
        if result['status'] not in {'complete','review_only'}:
            continue
        for summary in result['de']:
            cohort=summary['cohort_id'];path=run/omics/cohort/'de.csv';d=pd.read_csv(path)
            d['evidence_id']=[evidence_id(omics,cohort,f) for f in d.feature_id]
            d['artifact_path']=str(path.relative_to(run));d['row_key']=d.feature_id;d['claim_type']='association'
            d['evidence_layer']='de'
            registry.append(d)
            if cohort=='triple109':
                tables[omics]=d
                deltas[omics]=pd.read_csv(run/omics/cohort/'paired_delta.csv',index_col=0)
    if not registry:
        payload={'status':'blocked','hypotheses':[],'limitations':['No completed statistical domains']}
        write_json(run/'report/hypotheses.json',payload);return {'status':'blocked'}
    evidence=pd.concat(registry,ignore_index=True)
    # Open discovery retains every tested pathway, independently of the curated candidate axes.
    enrichment=[]
    for omics in ['transcriptomics','proteomics']:
        p=run/'pathways'/omics/'triple109/gsea_full.csv'
        if p.exists():
            d=pd.read_csv(p)
            enrichment.append(d[['pathway','NES','padj','size']].rename(columns={k:f'{k}_{omics}' for k in ['NES','padj','size']}))
    if len(enrichment)==2:
        discovery=enrichment[0].merge(enrichment[1],on='pathway',how='outer')
        discovery['both_fdr_pass']=(discovery.padj_transcriptomics<=cfg['analysis']['de']['padj'])&(discovery.padj_proteomics<=cfg['analysis']['de']['padj'])
        discovery['same_direction']=discovery.NES_transcriptomics*discovery.NES_proteomics>0
        discovery['interpretation']='exploratory_pathway_concordance_not_independent_validation'
        discovery.to_csv(out/'open_discovery_pathways.csv',index=False)
    if 'metabolomics' in tables:
        sensitivity=pd.read_csv(run/'metabolomics/same_batch108/de.csv')
        sensitivity=tables['metabolomics'][['feature_id','effect','padj','selected']].merge(sensitivity[['feature_id','effect','padj','selected']],on='feature_id',suffixes=('_109','_108'))
        sensitivity['same_direction']=sensitivity.effect_109*sensitivity.effect_108>0
        sensitivity.to_csv(out/'metabolomics_batch_sensitivity.csv',index=False)
    rn=None
    audit=run/'pathways/transcriptomics/triple109/gene_mapping_audit.csv'
    if 'transcriptomics' in tables and audit.exists():
        mapping=pd.read_csv(audit);mapping=mapping.loc[mapping.selected_representative & mapping.symbol.notna()]
        rn=tables['transcriptomics'].merge(mapping[['feature_id','symbol']],on='feature_id',validate='one_to_one')
    pr=tables.get('proteomics')
    mt=tables.get('metabolomics')
    clinical=pd.read_csv(run/'inputs/clinical_analysis.csv').set_index('patient_id')
    correlations=[];cross=pd.DataFrame()
    if rn is not None and pr is not None:
        cross=rn.merge(pr,left_on='symbol',right_on='feature_id',suffixes=('_rna','_protein'),how='outer')
        cross['gene']=cross.symbol.fillna(cross.feature_id_protein)
        cross['effect_pattern']=np.select([cross.effect_rna.isna(),cross.effect_protein.isna(),cross.effect_rna*cross.effect_protein>0,
                                          cross.effect_rna*cross.effect_protein<0],['rna_not_estimated','protein_not_estimated','same_direction','opposite_direction'],default='zero_effect')
        cross.to_csv(out/'cross_matrix.csv',index=False)
        for row in cross.dropna(subset=['feature_id_rna','feature_id_protein']).itertuples():
            if row.feature_id_rna not in deltas['transcriptomics'].index:continue
            patients=sorted(set(deltas['transcriptomics'].columns)&set(deltas['proteomics'].columns))
            x=deltas['transcriptomics'].loc[row.feature_id_rna,patients];y=deltas['proteomics'].loc[row.feature_id_protein,patients]
            correlations.append({'family':'rna_protein_all_mapped','x_omics':'transcriptomics','x_feature':row.feature_id_rna,
                                 'y_omics':'proteomics','y_feature':row.feature_id_protein,'gene':row.gene,
                                 **correlate(x,y),'claim_type':'association','cohort_id':'triple109'})
    axis_evidence=[];coverage=[]
    for axis in axes:
        entries=[]
        for gene in axis['genes']:
            for omics,frame,key in [('transcriptomics',rn,'symbol'),('proteomics',pr,'feature_id')]:
                hit=frame.loc[frame[key]==gene] if frame is not None else pd.DataFrame()
                coverage.append({'axis':axis['axis'],'entity':gene,'omics':omics,'measured':len(hit)>0,'status':'available' if len(hit) else 'not_measured_or_not_mapped'})
                for _,r in hit.iterrows():entries.append({**r.to_dict(),'axis':axis['axis'],'entity':gene,'identity_status':'gene_mapping_versioned'})
        for compound in axis['compounds']:
            matches=ann.loc[ann.compound_id==compound]
            coverage.append({'axis':axis['axis'],'entity':compound,'omics':'metabolomics','measured':len(matches)>0,
                             'status':';'.join(sorted(set(matches.identity_status))) if len(matches) else 'not_measured'})
            if mt is not None:
                for _,a in matches.iterrows():
                    for _,r in mt.loc[mt.feature_id==a.feature_id].iterrows():
                        entries.append({**r.to_dict(),'axis':axis['axis'],'entity':compound,'identity_status':a.identity_status,'compound_name':a.compound_name,'source_grade':a.source_grade})
        axis_evidence.extend(entries)
        if pr is not None and mt is not None:
            met_features=ann.loc[ann.compound_id.isin(axis['compounds']),'feature_id'].unique()
            patients=sorted(set(deltas['proteomics'].columns)&set(deltas['metabolomics'].columns))
            for gene in axis['genes']:
                if gene not in deltas['proteomics'].index:continue
                for feature in met_features:
                    if feature not in deltas['metabolomics'].index:continue
                    x=deltas['proteomics'].loc[gene,patients];y=deltas['metabolomics'].loc[feature,patients]
                    seed=cfg['study']['seed']+int(hashlib.sha256(f'{gene}|{feature}'.encode()).hexdigest()[:6],16)
                    correlations.append({'family':'axis_protein_metabolite','x_omics':'proteomics','x_feature':gene,'y_omics':'metabolomics','y_feature':feature,'gene':gene,'axis':axis['axis'],
                                         **correlate(x,y,seed,cfg['analysis']['integration']['bootstrap_replicates']),'claim_type':'association','cohort_id':'triple109'})
    ae=pd.DataFrame(axis_evidence);ae.to_csv(out/'axis_evidence.csv',index=False)
    pd.DataFrame(coverage).to_csv(out/'axis_coverage.csv',index=False)
    immune_path=run/'pathways/transcriptomics/triple109/immune_delta_axis_genes_excluded.csv'
    # Pre-declared sensitivity: estimated immune-proportion change (external atlas NNLS deconvolution)
    # as covariate. Estimate-based, labeled; never a measured cell fraction.
    immune_prop_delta=None
    prop_path=run/'scrna/deconvolution/cell_proportions_pancancer.csv'
    if scrna_ok and prop_path.exists():
        props=pd.read_csv(prop_path)
        immune_types=[c for c in ['CD4T cell','CD8T cell','NK cell','B cell','Myeloid cell'] if c in props.columns]
        if immune_types:
            props['immune_sum']=props[immune_types].sum(axis=1)
            t=props.loc[props.tissue=='tumor'].set_index('patient_id')['immune_sum']
            n=props.loc[props.tissue=='adjacent'].set_index('patient_id')['immune_sum']
            immune_prop_delta=(t-n).dropna()
    if immune_path.exists() and len(ae):
        immune=pd.read_csv(immune_path,index_col=0)
        for _,row in ae.drop_duplicates(['omics','feature_id']).iterrows():
            if row.feature_id not in deltas[row.omics].index:continue
            patients=sorted(set(immune.columns)&set(deltas[row.omics].columns))
            x=deltas[row.omics].loc[row.feature_id,patients]
            modes=[('unadjusted',None),('tumor_purity_sensitivity',clinical.loc[patients,'tumor_purity'])]
            if immune_prop_delta is not None:
                covar=immune_prop_delta.reindex(patients)
                if covar.notna().sum()>=4:
                    modes.append(('cell_proportion_adjusted',covar.to_numpy()))
            for program,y in immune[patients].iterrows():
                for mode,covar in modes:
                    correlations.append({'family':'axis_immune_program_'+mode,'x_omics':row.omics,'x_feature':row.feature_id,
                                         'y_omics':'transcriptomics','y_feature':program,'gene':row.entity,
                                         **correlate(x,y,covariate=covar),'claim_type':'association','cohort_id':'triple109',
                                         'limitation':'RNA-derived program; not cell fraction or independent evidence layer'})
    if correlations:
        corr=pd.DataFrame(correlations).drop_duplicates(['family','x_omics','x_feature','y_omics','y_feature'])
        adjust(corr,'family').to_csv(out/'patient_correlations.csv',index=False)
    # Pre-registered candidate-axis cohort tracking: prognosis and estimated T-cell correlations.
    # Runs only from the frozen prognosis tool outputs; absence degrades this section, not the bulk chain.
    prog_rows=pd.DataFrame(); tcell_rows=pd.DataFrame()
    if prognosis_ok:
        prog_rows, tcell_rows = candidate_axis_evidence(run, cfg, axes)
        extra=[frame for frame in [prog_rows, tcell_rows] if len(frame)]
        if extra:
            evidence=pd.concat([evidence, *extra], ignore_index=True)
            if evidence.evidence_id.duplicated().any():
                raise ValueError('Evidence ID collision after candidate-axis layers')
    evidence.to_csv(out/'evidence_registry.csv',index=False)
    # Candidate selection is deterministic and permits an empty list.
    hypotheses=[]
    if len(ae):
        for axis in axes:
            rows=ae.loc[ae.axis==axis['axis']].drop_duplicates('evidence_id')
            support=rows.loc[rows.selected.fillna(False)]
            omics=sorted(set(support.omics))
            if len(omics)<2:continue
            amb=rows.identity_status.eq('ambiguous').any()
            hypotheses.append({'id':axis['axis'],'origin':axis['origin'],
                'statement':f'候选假说：{axis["axis"]} 轴的跨组学组织变化可能与免疫相关表达程序共同变化，需细胞来源与扰动实验验证。',
                'claim_type':'hypothesis','evidence_ids':support.evidence_id.tolist(),'independent_omics':omics,
                'counterevidence_ids':rows.loc[~rows.selected.fillna(False),'evidence_id'].tolist(),
                'measurement_status':'ambiguous' if amb else 'putative','validation_status':'not_independently_validated',
                'testable_prediction':'身份确认后，干预候选酶或代谢物应改变预设免疫功能终点；若仅反映细胞组成，控制细胞比例后关联应减弱。',
                'experimental_design':'先以标准品/RT/MS/MS核验代谢物身份；定位表达细胞；采用预设剂量和对照的干预与救援实验，独立验证免疫功能。',
                'alternative_explanations':['肿瘤纯度与细胞组成差异','共同组织状态而非直接调控','注释或上游归一化不确定性'],
                'literature':[{'pmid':r['pmid'],'title':r['title'],'url':r['url'],'stance':'unreviewed_retrieval_match'} for r in literature['records'] if r['axis']==axis['axis']]})
    # ---- External scRNA atlas context: separate registry, never bulk evidence ----
    scrna_summary={'status':'absent'}
    scrna_registry_rows=None
    if scrna_ok:
        scrna_summary={'status':scrna_result['status'],'n_cells':scrna_result.get('n_cells',0),
                       'n_samples':scrna_result.get('n_samples',0),'scope':scrna_result.get('scope',''),
                       'analyses':scrna_result.get('analyses',[])}
        loc_path=run/'scrna/axis_localization.csv'
        axis_gene_set={g for a in axes for g in a['genes']}
        gene_axis={}
        for a in axes:
            for g in a['genes']:
                gene_axis.setdefault(g,[]).append(a['axis'])
        if loc_path.exists():
            loc=pd.read_csv(loc_path)
            axis_loc=loc.loc[loc.gene.isin(axis_gene_set)].copy()
            axis_loc['axis']=[';'.join(gene_axis.get(g,[])) for g in axis_loc.gene]
            axis_loc['evidence_id']=[evidence_id('scrna_context','external_atlas',f'{g}|{c}')
                                     for g,c in zip(axis_loc.gene,axis_loc.celltype)]
            axis_loc['artifact_path']='scrna/axis_localization.csv'
            axis_loc['boundary']='external_pan_cancer_atlas_context_not_independent_validation'
            axis_loc.to_csv(out/'scrna_axis_celltype.csv',index=False)
            registry_rows=axis_loc[['evidence_id','axis','gene','celltype','status','n_samples_tested',
                                    'donor_effect_median_diff','donor_pvalue','donor_padj','cell_level_auc',
                                    'cell_level_diff_pct','claim_type','cohort_id','artifact_path','boundary']]
            scrna_registry_rows=registry_rows.rename(columns={'gene':'entity','donor_effect_median_diff':'effect',
                                          'donor_pvalue':'pvalue','donor_padj':'padj'})
            scrna_registry_rows.to_csv(out/'scrna_evidence_registry.csv',index=False)
        paired_path=run/'scrna/deconvolution/deconvolution_paired.csv'
        if paired_path.exists():
            paired=pd.read_csv(paired_path)
            keep=['celltype','effect','pvalue','padj','n_pairs']
            summary=paired.pivot_table(index='celltype',columns='panel',values=[c for c in keep if c!='celltype'],aggfunc='first')
            summary.columns=[f'{v}_{p}' for v,p in summary.columns]
            summary=summary.reset_index()
            for a,b in [('effect_pancancer','effect_hcc_reference'),('effect_pancancer','effect_pancancer_top50')]:
                if a in summary and b in summary:
                    same=(np.sign(summary[a])==np.sign(summary[b])).astype(object)
                    same[summary[a].isna()|summary[b].isna()]=None
                    summary[f'same_direction_{b.split("effect_")[1]}']=same
            summary['claim_type']='estimate_not_measured_fraction'
            summary.to_csv(out/'deconvolution_paired_summary.csv',index=False)
    write_json(out/'scrna_status.json',scrna_summary)
    # Additive, validated projection only: legacy CSV and all statistics remain untouched.
    write_evidence_registry_v2(root, run, evidence, scrna_registry_rows)
    build_evidence_graph(root, run)

    if scrna_ok and len(hypotheses):
        loc_path=run/'scrna/axis_localization.csv'
        paired_path=run/'scrna/deconvolution/deconvolution_paired.csv'
        loc=pd.read_csv(loc_path) if loc_path.exists() else None
        paired=pd.read_csv(paired_path) if paired_path.exists() else None
        for h in hypotheses:
            axis=next(a for a in axes if a['axis']==h['id'])
            genes_ctx=[]
            if loc is not None:
                for gene in axis['genes']:
                    rows=loc.loc[loc.gene==gene]
                    if not len(rows):
                        genes_ctx.append({'gene':gene,'status':'not_in_localization_table'});continue
                    tested=rows.loc[rows.status=='tested']
                    if not len(tested):
                        genes_ctx.append({'gene':gene,'status':';'.join(sorted(set(rows.status)))});continue
                    best=tested.sort_values(['donor_padj','celltype']).iloc[0]
                    genes_ctx.append({'gene':gene,'status':'tested',
                        'top_celltype':best.celltype,'donor_effect_median_diff':None if pd.isna(best.donor_effect_median_diff) else float(best.donor_effect_median_diff),
                        'donor_padj':None if pd.isna(best.donor_padj) else float(best.donor_padj),
                        'pct_cells_expressing':None if pd.isna(best.pct_cells_expressing) else float(best.pct_cells_expressing),
                        'cell_level_auc':None if pd.isna(best.cell_level_auc) else float(best.cell_level_auc),
                        'n_celltypes_tested':int(len(tested))})
            dec_ctx=[]
            if paired is not None:
                for _,r in paired.loc[paired.panel=='pancancer'].sort_values('celltype').iterrows():
                    dec_ctx.append({'celltype':r.celltype,'effect':None if pd.isna(r.effect) else float(r.effect),
                                    'padj':None if pd.isna(r.padj) else float(r.padj),'n_pairs':int(r.n_pairs)})
            h['external_scrna_context']={'boundary':'external_pan_cancer_atlas_context_not_independent_validation',
                                         'genes':genes_ctx,'deconvolution':dec_ctx}

    payload={'status':'review_only','hypotheses':hypotheses,'limitations':[
        'Observational paired bulk tissue; not causal or cell-specific evidence.',
        'RNA immune programs are not independent omics or cell proportions.',
        'MetDNA grades are not MSI levels; 3MT feature is ambiguous.',
        'Nonsignificance is not absence or equivalence; opposite directions remain in cross_matrix.',
        'Protein centering axis and metabolomics processing history remain incompletely documented.',
        'Literature matches require semantic review and do not validate this cohort.',
        'External scRNA atlas context uses different donors (pan-cancer scope, user-confirmed); it is not independent validation and does not enter the two-omics rule.',
        'Deconvolution proportions are NNLS estimates from atlas marker signatures; malignant/epithelial signature transfer to HCC bulk is uncertain.',
        'Prognosis associations are single-cohort observational Cox models on tumor values per SD; recurrence-free survival is analyzed without a competing-risks model, and proportional-hazards diagnostics are reported but do not switch the model.',
        'T-cell signature scores are RNA-derived and share the RNA source with COMT RNA; they are not an independent evidence layer and do not enter the two-omics rule.']}
    validate_hypotheses(payload,evidence,root/'schemas/hypothesis.schema.json')
    write_json(run/'report/hypotheses.json',payload)
    return {'status':'review_only','n_evidence':len(evidence),'n_hypotheses':len(hypotheses),
            'scrna_context':scrna_summary['status'],
            'prognosis_context':prognosis_result.get('status') if prognosis_result else 'absent',
            'n_prognosis_evidence':int(len(prog_rows)),'n_tcell_evidence':int(len(tcell_rows))}

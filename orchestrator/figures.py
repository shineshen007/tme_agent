"""Vector-only figures with exact source tables and a manifest. PDF/SVG, no PNG."""
from pathlib import Path
import os
import numpy as np
import pandas as pd
from .io import artifact, write_json


PROPORTION_METADATA = {'biospecimen_id', 'patient_id', 'tissue', 'panel',
                       'claim_type', 'method', 'reference_panel'}


def resolve_font():
    """Arial when present; otherwise the metric-compatible Liberation Sans, recorded in the manifest."""
    from matplotlib import font_manager
    for name in ['Arial', 'Liberation Sans']:
        try:
            font_manager.findfont(name, fallback_to_default=False)
            return name, name != 'Arial'
        except ValueError:
            continue
    raise ValueError('Neither Arial nor metric-compatible Liberation Sans is available')


def render_figures(root, run):
    root,run=Path(root),Path(run)
    os.environ.setdefault('MPLCONFIGDIR',str(root/'.cache/matplotlib'))
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    font,substituted=resolve_font()
    plt.rcParams.update({'font.family':font,'font.size':8,'pdf.fonttype':42,'ps.fonttype':42,
                         'svg.fonttype':'none','axes.spines.top':False,'axes.spines.right':False})
    out=run/'figures';out.mkdir(exist_ok=True)
    source=out/'source_data';source.mkdir(exist_ok=True)
    panels=[]
    for omics,cohort in [('transcriptomics','full159'),('proteomics','full159'),('metabolomics','triple109')]:
        path=run/omics/cohort/'pca_source.csv'
        if not path.exists(): continue
        d=pd.read_csv(path);v=pd.read_csv(path.with_name('pca_variance.csv'))
        d.to_csv(source/f'{omics}_pca.csv',index=False)
        v.to_csv(source/f'{omics}_pca_variance.csv',index=False)
        fig,ax=plt.subplots(figsize=(3.5,3.1),layout='constrained')
        for _,pair in d.groupby('patient_id'):
            ax.plot(pair.PC1,pair.PC2,color='#B8B8B8',lw=.35,alpha=.45,zorder=1)
        for group,color in [('adjacent','#3B75AF'),('tumor','#CD584A')]:
            g=d.loc[d.tissue==group]
            ax.scatter(g.PC1,g.PC2,s=10,c=color,alpha=.8,linewidths=0,label=f'{group} (n={len(g)})',zorder=2)
        ax.set(xlabel=f'PC1 ({100*v.variance_fraction.iloc[0]:.1f}%)',ylabel=f'PC2 ({100*v.variance_fraction.iloc[1]:.1f}%)',title=f'{omics}: paired tissue PCA')
        ax.legend(frameon=False,fontsize=7,loc='best')
        for extension in ['pdf','svg']:
            target=out/f'{omics}_paired_pca.{extension}'
            fig.savefig(target)
            panels.append(artifact(run,target))
        plt.close(fig)
    panels.extend(render_scrna_figures(run,out,source,plt))
    write_json(out/'figure_manifest.json',{'status':'generated','format':'PDF_and_SVG_only',
       'font':f'{font} 8 pt','font_substituted_for_arial':substituted,
       'interpretation':'Descriptive panels of supplied/normalized expression and external-atlas context; all samples and patient links shown; not a batch correction or independent validation claim.',
       'script':'orchestrator/figures.py','artifacts':panels,'source_data':[artifact(run,p) for p in sorted(source.glob('*.csv'))]})
    return len(panels)


def render_scrna_figures(run,out,source,plt):
    """External-atlas context panels; drawn only when the branch produced tables."""
    panels=[]
    def save(fig,stem):
        for extension in ['pdf','svg']:
            target=out/f'{stem}.{extension}'
            fig.savefig(target)
            panels.append(artifact(run,target))
        plt.close(fig)

    axis_path=run/'integration/scrna_axis_celltype.csv'
    if axis_path.exists():
        d=pd.read_csv(axis_path)
        d.to_csv(source/'scrna_axis_celltype.csv',index=False)
        d=d.dropna(subset=['pct_cells_expressing'])
        if len(d):
            genes=list(dict.fromkeys(d.gene));cts=list(dict.fromkeys(d.celltype))
            fig,ax=plt.subplots(figsize=(1.7+0.55*len(cts),1.4+0.34*len(genes)),layout='constrained')
            xs=d.celltype.map(cts.index);ys=d.gene.map(genes.index)
            sc=ax.scatter(xs,ys,s=d.pct_cells_expressing*2.2,c=d.mean_expr_lognorm,cmap='viridis',
                          vmin=0,vmax=max(1e-9,float(np.nanmax(d.mean_expr_lognorm))),linewidths=0)
            ax.set_xticks(range(len(cts)));ax.set_xticklabels(cts,rotation=40,ha='right',fontsize=7)
            ax.set_yticks(range(len(genes)));ax.set_yticklabels(genes,fontsize=7)
            ax.set_title('Axis genes across atlas cell types (external context)',fontsize=8)
            cb=fig.colorbar(sc,ax=ax,pad=0.02);cb.set_label('mean lognorm expr',fontsize=7);cb.ax.tick_params(labelsize=7)
            ax.set_xlabel('cell type (dot size = % cells expressing)',fontsize=7)
            save(fig,'scrna_axis_dotplot')

    prop_path=run/'scrna/deconvolution/cell_proportions_pancancer.csv'
    if prop_path.exists():
        props=pd.read_csv(prop_path)
        props.to_csv(source/'scrna_deconvolution_proportions_pancancer.csv',index=False)
        cts=[c for c in props.columns if c not in PROPORTION_METADATA]
        if cts:
            ncol=3;nrow=int(np.ceil(len(cts)/ncol))
            fig,axes=plt.subplots(nrow,ncol,figsize=(2.1*ncol,1.9*nrow),layout='constrained',squeeze=False)
            for i,cty in enumerate(cts):
                ax=axes[i//ncol][i%ncol]
                wide=props.pivot(index='patient_id',columns='tissue',values=cty)
                for _,r in wide.iterrows():
                    if np.isfinite(r.get('adjacent',np.nan)) and np.isfinite(r.get('tumor',np.nan)):
                        ax.plot([0,1],[r['adjacent'],r['tumor']],color='#B8B8B8',lw=.3,alpha=.4,zorder=1)
                for x,tissue,color in [(0,'adjacent','#3B75AF'),(1,'tumor','#CD584A')]:
                    v=wide[tissue].dropna() if tissue in wide else pd.Series(dtype=float)
                    ax.scatter(np.full(len(v),x),v,s=4,c=color,linewidths=0,alpha=.7,zorder=2)
                    if len(v): ax.scatter([x],[v.mean()],marker='_',s=60,c='black',zorder=3)
                ax.set_xticks([0,1]);ax.set_xticklabels(['adj','tum'],fontsize=6)
                note=' (transfer uncertain)' if cty in {'Malignant cell','Epithelial cell'} else ''
                ax.set_title(cty+note,fontsize=7)
                ax.tick_params(labelsize=6)
            for j in range(len(cts),nrow*ncol): axes[j//ncol][j%ncol].axis('off')
            fig.suptitle('NNLS-estimated proportions, paired T-N (estimates, not measured fractions)',fontsize=8)
            save(fig,'scrna_deconvolution_paired')

    gs_path=run/'scrna/geneset_celltype.csv'
    if gs_path.exists():
        gs=pd.read_csv(gs_path)
        if len(gs) and 'mean_score' in gs:
            gs.to_csv(source/'scrna_geneset_celltype.csv',index=False)
            mat=gs.pivot_table(index='geneset',columns='celltype',values='mean_score',aggfunc='first')
            mat=mat.dropna(how='all').dropna(axis=1,how='all')
            if mat.size:
                fig,ax=plt.subplots(figsize=(1.6+0.62*mat.shape[1],1.3+0.42*mat.shape[0]),layout='constrained')
                vmax=float(np.nanmax(np.abs(mat.to_numpy()))) or 1.0
                im=ax.imshow(mat.to_numpy(),cmap='RdBu_r',vmin=-vmax,vmax=vmax,aspect='auto')
                ax.set_xticks(range(mat.shape[1]));ax.set_xticklabels(mat.columns,rotation=40,ha='right',fontsize=7)
                ax.set_yticks(range(mat.shape[0]));ax.set_yticklabels(mat.index,fontsize=7)
                for r in range(mat.shape[0]):
                    for c in range(mat.shape[1]):
                        v=mat.iloc[r,c]
                        if np.isfinite(v): ax.text(c,r,f'{v:.2f}',ha='center',va='center',fontsize=5.5)
                cb=fig.colorbar(im,ax=ax,pad=0.02);cb.set_label('ssGSEA score (pseudobulk mean)',fontsize=7);cb.ax.tick_params(labelsize=7)
                ax.set_title('Bulk DE gene sets across atlas cell types (descriptive)',fontsize=8)
                save(fig,'scrna_geneset_heatmap')

    umap_path=run/'scrna/umap_source.csv'
    if umap_path.exists():
        u=pd.read_csv(umap_path)
        u.to_csv(source/'scrna_umap_source.csv',index=False)
        if len(u):
            cts=list(dict.fromkeys(u.celltype))
            cmap=plt.get_cmap('tab10' if len(cts)<=10 else 'tab20')
            colors={c:cmap(i%max(cmap.N,1)) for i,c in enumerate(cts)}
            fig,ax=plt.subplots(figsize=(3.6,3.2),layout='constrained')
            for c in cts:
                g=u.loc[u.celltype==c]
                ax.scatter(g.UMAP_1,g.UMAP_2,s=.4,c=[colors[c]],alpha=.45,linewidths=0,label=c,rasterized=True)
            ax.set(xlabel='UMAP_1',ylabel='UMAP_2',title='External atlas UMAP (provided embedding, subsampled)')
            ax.legend(frameon=False,fontsize=5.5,loc='center left',bbox_to_anchor=(1.01,0.5),markerscale=6)
            save(fig,'scrna_umap')
    return panels

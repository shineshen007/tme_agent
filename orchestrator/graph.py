"""Executable LangGraph with deterministic tools and disjoint parallel results."""
from pathlib import Path
import yaml
from langgraph.graph import StateGraph, START, END
from .state import AgentState
from .io import write_json
from .statistics import analyze_domain
from .scrna import analyze_scrna
from .prognosis import analyze_prognosis
from .checkers import validate_schema, verify_artifacts, validate_de
from .pathways import run_pathways
from .integration import integrate
from .literature import run_literature
from .loop_graph import attach_reasoning_loop
from .dossiers import build_target_dossiers


def build_graph(root,run,cfg,transport=None):
    root,run=Path(root),Path(run)
    g=StateGraph(AgentState)
    axes=yaml.safe_load((root/'knowledge/tme_metabolism_axes.yaml').read_text())['axes']
    def planner(state):
        plan={'schema_version':'1.0','design':'paired','reference':'adjacent','seed':cfg['study']['seed'],
              'tasks':[{'id':o,'cohorts':['full159','triple109'] if o!='metabolomics' else ['triple109','same_batch108'],
                        'tool':'scripts/02_statistics.R','depends_on':['validated_inputs']} for o in ['transcriptomics','proteomics','metabolomics']]
                     +[{'id':'scrna','cohorts':['external_atlas'],'tool':'scripts/05_scrna_celltype.R',
                        'depends_on':['validated_inputs','transcriptomics','proteomics'],
                        'evidence_boundary':'external_context_not_independent_validation'}]
                     +[{'id':'pathways','cohorts':['triple109'],'tool':'scripts/03_pathways.R',
                        'depends_on':['validated_inputs','transcriptomics','proteomics'],
                        'note':'also emits the frozen gene-mapping audit consumed by the prognosis branch'}]
                     +[{'id':'prognosis','cohorts':['triple109','same_batch108'],'tool':'scripts/06_prognosis.R',
                        'depends_on':['validated_inputs','transcriptomics','proteomics','metabolomics','scrna','pathways'],
                        'evidence_boundary':'candidate_axis_tracking_prespecified_not_discovery'}]
                     +[{'id':'literature','tool':'scripts/04_literature.py','depends_on':['integration']}]
                     +[{'id':'agent_layer','tool':'orchestrator/loop_graph.py',
                        'depends_on':['integration','literature'],
                        'authority':'model_generated_interpretation_only_numbers_injected_deterministically',
                        'enabled':bool(cfg['llm']['enabled'])}],
              'integration':{'depends_on':['transcriptomics','proteomics','metabolomics','scrna','pathways','prognosis'],'cohort':'triple109'},
              'llm_enabled':bool(cfg['llm']['enabled']),'parameters':cfg['analysis']}
        write_json(run/'plan/PLAN.json',plan)
        return {'plan':plan}
    g.add_node('hub_planner',planner)
    keys={'transcriptomics':'rna_result','proteomics':'protein_result','metabolomics':'metabolite_result'}
    for omics,key in keys.items():
        def spoke(state,omics=omics,key=key):
            result=analyze_domain(root,run,cfg,omics)
            validate_schema(result,root/'schemas/spoke_output.schema.json')
            verify_artifacts(run,result['artifacts'])
            for a in result['artifacts']:
                if a['path'].endswith('/de.csv'):validate_de(run/a['path'])
            return {key:result}
        g.add_node(omics,spoke)
        g.add_edge('hub_planner',omics)
    def scrna_spoke(state):
        # Runs after transcriptomics/proteomics so frozen bulk DE candidates exist;
        # tolerates their failure (the R tool degrades gene-set sections to blocked).
        result=analyze_scrna(root,run,cfg,state.get('rna_result'),state.get('protein_result'))
        validate_schema(result,root/'schemas/scrna_output.schema.json')
        verify_artifacts(run,result['artifacts'])
        return {'scrna_result':result}
    g.add_node('scrna',scrna_spoke)
    g.add_edge('transcriptomics','scrna')
    g.add_edge('proteomics','scrna')
    def pathways_node(state):
        # Deterministic pathway/enrichment tools also emit the frozen gene-mapping audit that the
        # prognosis branch resolves axis genes from, so they must run before it.
        results={o:state[k] for o,k in keys.items()}
        return {'pathway_status':run_pathways(root,run,cfg,results)}
    g.add_node('hub_pathways',pathways_node)
    g.add_edge(list(keys)+['scrna'],'hub_pathways')
    def prognosis_spoke(state):
        # Pre-registered candidate-axis tracking: frozen panel and thresholds; runs after
        # the bulk spokes (expression values), scrna (estimated proportions) and pathways
        # (frozen gene-mapping audit).
        result=analyze_prognosis(root,run,cfg,scrna_result=state.get('scrna_result'))
        validate_schema(result,root/'schemas/prognosis_output.schema.json')
        verify_artifacts(run,result['artifacts'])
        return {'prognosis_result':result}
    g.add_node('prognosis',prognosis_spoke)
    g.add_edge('hub_pathways','prognosis')
    def integrator(state):
        results={o:state[k] for o,k in keys.items()}
        result=integrate(root,run,cfg,results,scrna_result=state.get('scrna_result'),
                         prognosis_result=state.get('prognosis_result'))
        return {'integration_status':result}
    g.add_node('hub_integrator',integrator)
    def literature_node(state):
        if not cfg['llm']['enabled']:
            return {'literature_result':{'status':'disabled'}}
        try:
            summary=run_literature(root,run,cfg,axes)
        except Exception as exc:  # retrieval failure is recorded, never fatal to statistics
            summary={'status':'failed','error':str(exc),'n_records':0}
        return {'literature_result':summary}
    g.add_node('hub_literature',literature_node)
    def disabled_agents(state):
        write_json(run/'report/agent/agent_manifest.json',
                   {'status':'disabled','reason':'llm.enabled=false',
                    'authority':'model_generated_interpretation_only_numbers_injected_deterministically'})
        return {'agent_result':{'status':'disabled','model':None,'n_hypotheses':0}}
    g.add_node('hub_agents_disabled',disabled_agents)
    def dossiers_node(state):
        # Deterministic aggregation of frozen evidence into one dossier per proposed target.
        # Publication to the project-root result directory happens in runner after the run is
        # complete (never from inside the graph), so a failed run cannot leave a partial deliverable.
        if not cfg.get('deliverables',{}).get('dossiers',{}).get('enabled',True):
            return {'dossier_result':{'status':'disabled','n_targets':0,'targets':[],'errors':[]}}
        try:
            result=build_target_dossiers(root,run,cfg)
        except Exception as exc:
            return {'dossier_result':{'status':'failed','n_targets':0,'targets':[],
                                      'errors':[{'target':None,'message':str(exc)}]}}
        return {'dossier_result':result}
    g.add_node('hub_dossiers',dossiers_node)
    g.add_edge(START,'hub_planner')
    g.add_edge('prognosis','hub_integrator')
    g.add_edge('hub_integrator','hub_literature')
    attach_reasoning_loop(g,root,run,cfg,axes,transport=transport)
    g.add_conditional_edges('hub_literature',lambda state: 'on' if cfg['llm']['enabled'] else 'off',
                            {'on':'agent_preparation','off':'hub_agents_disabled'})
    g.add_edge('hub_agents_disabled','hub_dossiers')
    g.add_edge('hub_dossiers',END)
    return g.compile()

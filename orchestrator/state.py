"""Each parallel domain owns a distinct key; no concurrent shared-dict writes."""
from typing import TypedDict

class AgentState(TypedDict, total=False):
    run_id: str
    plan: dict
    rna_result: dict
    protein_result: dict
    metabolite_result: dict
    scrna_result: dict
    prognosis_result: dict
    pathway_status: dict
    integration_status: dict
    literature_result: dict
    agent_result: dict
    dossier_result: dict
    analyst_outputs: dict
    frozen_snapshot: str
    frozen_snapshot_sha256: str
    reasoning_iteration: int
    active_evidence_ids: list
    lookup_results: list
    request_history: list
    evidence_clusters: dict
    reasoner_internal: dict
    synthesis_draft: dict
    rejected_drafts: list
    critic_review: dict
    prior_review: dict
    reanalysis_requests: list
    reanalysis_results: list
    evidence_changes: dict
    loop_action: str
    loop_error: str

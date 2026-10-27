"""Offline end-to-end research continuation; model/retrieval fixtures are not science validation."""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from multiomics_tui.task_sessions import TaskStore
from orchestrator.knowledge import KnowledgeService
from orchestrator.research_literature import _passage, _seal
from orchestrator.research_pipeline import run_task_research, verify_research_attempt


CFG = {"llm": {"provider": "fixture", "model": "offline", "temperature": 0,
               "max_tokens": 4000, "token_budget": 20000},
       "reasoning": {"max_iterations": 2}, "knowledge": {"shared_enabled": False}}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def completed_task(root, omics=("transcriptomics",), question=None):
    """Use actual approval state and complete output manifests without executing generated code."""
    source = root / "data/raw/metadata.csv"
    source.parent.mkdir(parents=True, exist_ok=True)
    if not source.exists():
        source.write_text("sample,patient\nPRIVATE_SAMPLE_A,PRIVATE_PATIENT_A\n")
    inputs = [{"path": "data/raw/metadata.csv", "sha256": sha(source), "bytes": source.stat().st_size}]
    specs = []
    for assay in omics:
        specs.append({"path": f"tables/{assay}.csv", "layer": "de", "omics": assay,
            "entity_type": "protein" if assay == "proteomics" else "gene", "namespace": "fixture.symbol",
            "columns": {"entity_id": "entity", "effect": "effect", "p_value": "p", "adjusted_p": "padj", "n": "n", "selected": "selected"},
            "effect_scale": "log2_fold_change", "row_key": ["entity"], "measured_omics": [assay],
            "evidence_context": "measured_evidence"})
    outputs = [s["path"] for s in specs] + ["reports/analysis.txt"]
    context = {"question": question or "Explain expression changes and possible mechanism in colorectal cancer",
               "disease": "colorectal cancer", "species": "Homo sapiens", "tissue": "colon",
               "analysis_unit": "patient", "cohort_id": "fixture_cohort",
               "contrast": {"numerator": "tumor", "denominator": "adjacent"},
               "limitations": ["Synthetic engineering fixture; no biological acceptance claim."]}
    plan = {"goal": context["question"], "analysis_unit": "patient", "method": "fixture projection",
            "parameters": [], "inputs": inputs, "limitations": context["limitations"],
            "steps": [{"id": "step_01", "language": "python", "purpose": "fixture", "outputs": outputs}],
            "input_bindings": {"matrices": [{"metadata_path": "data/raw/metadata.csv", "sample_key": "sample", "pair_key": "patient"}]},
            "research": {"enabled": True, "context": context, "evidence_outputs": specs}}
    store = TaskStore(root)
    state = store.create(plan["goal"])
    state = store.save_plan(state["task_id"], plan)
    state = store.approve(state["task_id"], state["plan_hash"])
    folder = root / "analysis" / state["task_id"]
    for name in ("scripts", "tables", "reports"):
        (folder / name).mkdir(parents=True)
    for spec in specs:
        (folder / spec["path"]).write_text("entity,effect,p,padj,n,selected\nGENE_A,-1.2,0.001,0.01,20,TRUE\nGENE_B,0.1,0.8,0.9,20,FALSE\nGENE_C,NA,NA,NA,NA,FALSE\n")
    (folder / "reports/analysis.txt").write_text("All synthetic results including negative and NA rows.\n")
    script = folder / "scripts/step_01.py"
    script.write_text("# approved synthetic fixture, never executed\n")
    scripts = [{"step_id": "step_01", "path": "scripts/step_01.py", "sha256": sha(script), "preflight_passed": True}]
    output_records = [{"path": p, "sha256": sha(folder / p), "bytes": (folder / p).stat().st_size} for p in outputs]
    write_json(folder / "code_manifest.json", {"plan_hash": state["plan_hash"], "status": "succeeded", "inputs": inputs, "scripts": scripts})
    write_json(folder / "result_manifest.json", {"task_id": state["task_id"], "plan_hash": state["plan_hash"], "inputs": inputs, "scripts": scripts, "outputs": output_records})
    state["status"] = "succeeded"
    store.save(state)
    return store, state["task_id"], folder


class StubLiterature:
    def __init__(self, status="complete"):
        self.status, self.calls = status, []
        self.record = _seal({"source_id": "PMID:12345", "pmid": "12345", "doi": None,
            "title": "Synthetic expression context fixture", "year": "2024", "url": "https://pubmed.ncbi.nlm.nih.gov/12345/",
            "publication_types": ["Journal Article"], "retraction_metadata": [],
            "passages": [_passage("abstract:1", "Expression changes accompanied altered cell composition in this synthetic model.")]})

    def search(self, queries, context=None):
        self.calls.append((copy.deepcopy(queries), copy.deepcopy(context)))
        return {"status": self.status, "records": [copy.deepcopy(self.record)] if self.status == "complete" else [],
                "queries": [{"query": q, "provider": "fixture", "status": self.status} for q in queries],
                "errors": [{"error": "offline fixture"}] if self.status == "offline" else []}


class StubClient:
    def __init__(self, *, hypothesis=False, literature_claim=False, fail_role=None, on_call=None,
                 critic_decision="accept"):
        self.hypothesis, self.literature_claim = hypothesis, literature_claim
        self.fail_role, self.on_call, self.critic_decision = fail_role, on_call, critic_decision
        self.calls = []

    @staticmethod
    def interpretation(content):
        return {"id": "I1", "kind": "biological_inference",
                "statement": "Expression changes may reflect altered cell composition or a candidate mechanism.",
                "evidence_ids": [r["evidence_id"] for r in content["observations"]],
                "knowledge_ids": [r["entry_id"] for r in content.get("reviewed_knowledge", {}).get("entries", [])],
                "alternative_explanations": ["Cell composition could differ."],
                "limitations": ["Observational abundance does not establish activity."]}

    def chat_json(self, *, role, system, user, schema_name, validator):
        content = json.loads(user)
        self.calls.append({"role": role, "content": content, "system": system})
        assert "PRIVATE_SAMPLE_A" not in user and "PRIVATE_PATIENT_A" not in user
        if self.on_call:
            self.on_call(role, content)
        if role == self.fail_role:
            raise RuntimeError("injected model failure")
        if role == "analyst_research":
            result = {"interpretations": [self.interpretation(content)], "limitations": ["Synthetic fixture."]}
        elif role == "librarian":
            result = {"queries": ["colorectal cancer expression cell composition contradictory evidence"]}
        elif role == "evidence_integrator":
            result = {"clusters": [{"id": "EC1", "topic": "Expression context",
                "supporting_evidence_ids": [r["evidence_id"] for r in content["observations"]],
                "counterevidence_ids": [], "interpretation": "Expression context remains observational.",
                "missing_links": ["Activity has not been measured."],
                "direction_note": "Opposite molecular directions alone do not establish contradiction."}]}
        elif role == "hypothesis_reasoner":
            result = {"interpretations": [self.interpretation(content)], "hypotheses": [],
                      "literature_claims": [], "novelty": [], "limitations": ["Mechanistic confirmation is absent."]}
            if self.hypothesis:
                ids = [r["evidence_id"] for r in content["observations"] if r["observation"]["selected"] is True]
                result["hypotheses"] = [{"id": "H1", "kind": "hypothesis",
                    "statement": "The candidate expression change may accompany a perturbable cellular program.",
                    "evidence_ids": ids, "counterevidence_ids": [], "knowledge_ids": [],
                    "conditions": ["The association must persist after accounting for composition."],
                    "testable_predictions": [{"prediction": "Perturbation may alter the candidate readout.",
                        "readout": "Candidate molecular readout", "expected_if_true": "Reproducible shift with rescue",
                        "expected_if_false": "No reproducible shift despite target engagement"}],
                    "validation_experiments": [{"hypothesis_id": "H1", "model_system": "Human colonic organoids",
                        "model_rationale": "Verify relevance in disease context before use.", "perturbation": "Candidate-specific loss of function",
                        "support_criterion": "Concordant target engagement and rescue", "falsification_criterion": "Readout unchanged despite verified target engagement",
                        "replication_unit": "Independent donor-derived organoid line", "sample_size_basis": "Pilot variance and an approved power analysis are required.",
                        "rescue_or_orthogonal": "Rescue with an orthogonal readout", "feasibility": "Requires source and model verification",
                        "controls": ["Vehicle", "Nontargeting perturbation", "Rescue"], "readouts": ["Target engagement", "Candidate readout"],
                        "knowledge_ids": [], "source_ids": [], "unknowns": ["Model suitability requires review."]}],
                    "alternative_explanations": ["Composition may explain the association."], "unknowns": ["Cell intrinsic mechanism remains untested."]}]
            if self.literature_claim:
                result["literature_claims"] = [{"claim_id": "I1", "source_id": "PMID:12345", "relation": "supports",
                    "directness": "indirect", "quote": "Expression changes accompanied altered cell composition",
                    "locator": "abstract:1", "rationale": "This motivates a competing explanation only.",
                    "context_match": {k: "unknown" for k in ("species", "disease", "tissue", "model", "assay", "outcome")}}]
        elif role == "critic":
            result = {"decision": self.critic_decision, "issues": [], "analysis_requests": [], "limitations": [],
                      "literature_review": [{"claim_id": r["claim_id"], "source_id": r["source_id"],
                         "decision": "supported_as_stated", "rationale": "Indirect contextual support only."}
                        for r in content["draft"]["literature_claims"]]}
            if self.critic_decision == "request_analysis":
                result["analysis_requests"] = ["Assess composition with an explicitly approved additional analysis."]
            elif self.critic_decision in {"block", "revise_reasoning"}:
                result["issues"] = [{"claim_id": "I1", "severity": "blocking", "message": "Additional interpretation review required."}]
        else:
            raise AssertionError(role)
        validator(result)
        return {"parsed": result}


def frozen_analysis(folder):
    return {str(p.relative_to(folder)): sha(p) for p in folder.rglob("*")
            if p.is_file() and "research" not in p.relative_to(folder).parts}


def run(root, task_id, client=None, literature=None):
    return run_task_research(root, task_id, cfg=CFG, client=client or StubClient(),
                             literature_service=literature or StubLiterature())


@pytest.mark.parametrize("omics", [("transcriptomics",), ("transcriptomics", "proteomics")])
def test_single_and_multiomics_accept_empty_hypotheses(tmp_path, omics):
    store, task_id, folder = completed_task(tmp_path, omics)
    before = frozen_analysis(folder)
    client = StubClient()
    result = run(tmp_path, task_id, client)
    assert result["research_status"] == "complete", result
    assert result["scientific_status"] == "EXPLORATORY"
    attempt = Path(result["attempt"])
    synthesis = json.loads((attempt / "synthesis.json").read_text())
    assert synthesis["hypotheses"] == [] and len(synthesis["interpretations"]) == 1
    context = json.loads((attempt / "research_context.json").read_text())
    assert len(context["evidence"]) == 3 * len(omics)
    assert {r["source"]["omics"] for r in context["evidence"]} == set(omics)
    assert "未形成满足" in (attempt / "research_report.md").read_text()
    assert before == frozen_analysis(folder) and store.get(task_id)["status"] == "succeeded"
    assert [x["role"] for x in client.calls] == ["analyst_research", "librarian", "evidence_integrator", "hypothesis_reasoner", "critic"]
    assert verify_research_attempt(tmp_path, attempt)["status"] == "verified"


@pytest.mark.parametrize("omics,basis", [(('transcriptomics',), 'single_omics_or_derived'),
                                         (('transcriptomics', 'proteomics'), 'multiomics')])
def test_hypotheses_keep_falsifiable_design_and_validation_boundary(tmp_path, omics, basis):
    _, task_id, _ = completed_task(tmp_path, omics)
    result = run(tmp_path, task_id, StubClient(hypothesis=True, literature_claim=True))
    assert result["research_status"] == "complete", result
    draft = json.loads((Path(result["attempt"]) / "synthesis.json").read_text())
    hypothesis = draft["hypotheses"][0]
    assert hypothesis["evidence_basis"] == basis
    assert hypothesis["measured_omics"] == sorted(omics)
    assert hypothesis["validation_status"] == "not_independently_validated"
    assert hypothesis["validation_experiments"][0]["hypothesis_id"] == hypothesis["id"]
    assert draft["literature_claims"][0]["directness"] == "indirect"


def test_failure_preserves_completed_analysis_and_retry_uses_new_attempt(tmp_path):
    store, task_id, folder = completed_task(tmp_path)
    before = frozen_analysis(folder)
    first = run(tmp_path, task_id, StubClient(fail_role="hypothesis_reasoner"))
    assert first["research_status"] == "blocked" and "injected model failure" in first["error"]
    failed = Path(first["attempt"])
    failed_hashes = {p: sha(p) for p in failed.rglob("*") if p.is_file()}
    second = run(tmp_path, task_id)
    assert second["research_status"] == "complete", second
    assert first["attempt"] != second["attempt"]
    assert failed_hashes == {p: sha(p) for p in failed.rglob("*") if p.is_file()}
    assert before == frozen_analysis(folder) and store.get(task_id)["status"] == "succeeded"


@pytest.mark.parametrize("status", ["offline", "no_results"])
def test_missing_literature_preserves_partial_explanation_without_novelty(tmp_path, status):
    _, task_id, _ = completed_task(tmp_path)
    result = run(tmp_path, task_id, literature=StubLiterature(status))
    assert result["research_status"] == "partial", result
    draft = json.loads((Path(result["attempt"]) / "synthesis.json").read_text())
    assert draft["novelty"] == [] and draft["interpretations"]


def test_snapshot_tampering_during_reasoning_blocks_without_rewriting_analysis(tmp_path):
    _, task_id, folder = completed_task(tmp_path)
    before = frozen_analysis(folder)
    def tamper(role, _content):
        if role == "evidence_integrator":
            snapshot = next((folder / "research").glob("attempt_*/knowledge_snapshot.json"))
            snapshot.write_text('{"entries": [], "tampered": true}')
    result = run(tmp_path, task_id, StubClient(on_call=tamper))
    assert result["research_status"] == "blocked", result
    assert "Frozen research source changed" in result["error"]
    assert before == frozen_analysis(folder)


def test_completed_attempt_verification_detects_tampering(tmp_path):
    _, task_id, _ = completed_task(tmp_path)
    result = run(tmp_path, task_id)
    attempt = Path(result["attempt"])
    (attempt / "knowledge_snapshot.json").write_text("{}")
    with pytest.raises(ValueError, match="Research artifact hash mismatch"):
        verify_research_attempt(tmp_path, attempt)


def test_reviewed_candidate_is_retrieved_by_next_task_only_after_human_review(tmp_path):
    _, first_id, _ = completed_task(tmp_path)
    first = run(tmp_path, first_id)
    assert first["research_status"] == "complete", first
    service = KnowledgeService(tmp_path / "knowledge/wiki")
    proposals = service.pending()
    assert len(proposals) == 1
    assert service.search("expression mechanism") == []
    approved = service.review(proposals[0]["id"], proposals[0]["sha256"], "approve")
    _, second_id, _ = completed_task(tmp_path)
    client = StubClient()
    second = run(tmp_path, second_id, client)
    assert second["research_status"] == "complete", second
    snapshot = json.loads((Path(second["attempt"]) / "knowledge_snapshot.json").read_text())
    assert approved["entry_id"] in {e["entry_id"] for e in snapshot["entries"]}
    analyst = next(c for c in client.calls if c["role"] == "analyst_research")
    assert approved["entry_id"] in {e["entry_id"] for e in analyst["content"]["reviewed_knowledge"]["entries"]}
    assert all(e["kind"] == "biological_inference" for e in snapshot["entries"])


def test_analysis_request_is_recorded_without_running_new_statistics(tmp_path):
    store, task_id, folder = completed_task(tmp_path)
    before = frozen_analysis(folder)
    result = run(tmp_path, task_id, StubClient(critic_decision="request_analysis"))
    assert result["research_status"] == "partial", result
    requests = json.loads((Path(result["attempt"]) / "analysis_requests.json").read_text())
    assert requests["status"] == "requires_new_approved_plan" and requests["requests"]
    assert before == frozen_analysis(folder) and store.get(task_id)["status"] == "succeeded"


def test_reasoning_revision_budget_stops_and_retains_both_iterations(tmp_path):
    _, task_id, _ = completed_task(tmp_path)
    result = run(tmp_path, task_id, StubClient(critic_decision="revise_reasoning"))
    assert result["research_status"] == "blocked", result
    assert result["error"] == "reasoning_iteration_limit"
    attempt = Path(result["attempt"])
    assert (attempt / "iteration_01/review.json").is_file()
    assert (attempt / "iteration_02/review.json").is_file()


def test_finalization_failure_cannot_report_research_complete(tmp_path, monkeypatch):
    from orchestrator import research_pipeline
    store, task_id, folder = completed_task(tmp_path)
    before = frozen_analysis(folder)
    def fail_report(*args, **kwargs):
        raise OSError("injected report write failure")
    monkeypatch.setattr(research_pipeline, "render_research_report", fail_report)
    result = run(tmp_path, task_id)
    assert result["research_status"] == "blocked", result
    assert store.get(task_id)["research_status"] == "blocked"
    assert before == frozen_analysis(folder)


def test_attempt_manifest_cannot_drop_a_frozen_artifact(tmp_path):
    _, task_id, _ = completed_task(tmp_path)
    result = run(tmp_path, task_id)
    attempt = Path(result["attempt"])
    manifest_path = attempt / "research_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["artifacts"] = [a for a in manifest["artifacts"] if a["path"] != "knowledge_snapshot.json"]
    write_json(manifest_path, manifest)
    (attempt / "knowledge_snapshot.json").write_text("{}")
    with pytest.raises(ValueError):
        verify_research_attempt(tmp_path, attempt)


def test_private_context_is_blocked_before_model_or_literature_calls(tmp_path):
    _, task_id, _ = completed_task(tmp_path, question="Explain PRIVATE_PATIENT_A molecular changes")
    client, literature = StubClient(), StubLiterature()
    result = run(tmp_path, task_id, client, literature)
    assert result["research_status"] == "blocked", result
    assert "private identifier" in result["error"]
    assert client.calls == literature.calls == []

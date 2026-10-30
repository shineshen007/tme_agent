"""Offline scientific contracts: approval, complete provenance and honest unknowns."""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from multiomics_tui.task_sessions import TaskStore
from orchestrator.evidence_graph import build_context_evidence_graph
from orchestrator.research_context import (
    LegacyResearchAdapter, TaskResearchAdapter, validate_research_plan,
    verify_context_sources,
)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def research_spec():
    return {"enabled": True, "context": {
        "question": "Which observed changes warrant mechanistic follow-up?",
        "disease": "colorectal cancer", "species": "Homo sapiens", "tissue": "colon",
        "analysis_unit": "patient", "cohort_id": "cohort_example",
        "contrast": {"numerator": "tumor", "denominator": "adjacent"}, "limitations": ["Exploratory"]},
        "evidence_outputs": [{"path": "tables/de.csv", "layer": "de", "omics": "transcriptomics",
            "entity_type": "gene", "namespace": "HGNC.symbol", "columns": {
                "entity_id": "gene", "effect": "log2FC", "p_value": "p", "adjusted_p": "padj", "n": "n"},
            "effect_scale": "log2_fold_change", "row_key": ["gene"],
            "measured_omics": ["transcriptomics"], "evidence_context": "measured_evidence"}]}


def completed_task(root, *, research=None, table=None, privacy=False):
    source = root / "data/raw/metadata.csv"
    source.parent.mkdir(parents=True)
    source.write_text("sample,patient\nTCGA-AA-0001-01A,TCGA-AA-0001\n")
    inputs = [{"path": "data/raw/metadata.csv", "sha256": digest(source), "bytes": source.stat().st_size}]
    plan = {"goal": "Inspect an explicitly bound synthetic result", "analysis_unit": "patient",
            "method": "fixture", "parameters": [], "inputs": inputs,
            "steps": [{"id": "step_01", "language": "python", "outputs": ["tables/de.csv", "reports/details.txt"]}],
            "limitations": [], "research": research or research_spec()}
    if privacy:
        plan["input_bindings"] = {"matrices": [{"metadata_path": "data/raw/metadata.csv", "sample_key": "sample", "pair_key": "patient"}]}
    store = TaskStore(root)
    state = store.create(plan["goal"])
    state = store.save_plan(state["task_id"], plan)
    state = store.approve(state["task_id"], state["plan_hash"])
    folder = root / "analysis" / state["task_id"]
    for name in ("tables", "reports", "scripts"):
        (folder / name).mkdir(parents=True)
    (folder / "tables/de.csv").write_text(table or "gene,log2FC,p,padj,n\nGENE1,-1.2,0.001,0.01,20\nGENE2,0.1,0.8,0.9,20\nGENE3,NA,NA,NA,NA\n")
    (folder / "reports/details.txt").write_text("All synthetic results preserved.\n")
    script = folder / "scripts/step_01.py"
    script.write_text("# synthetic script fixture\n")
    scripts = [{"step_id": "step_01", "path": "scripts/step_01.py", "sha256": digest(script), "preflight_passed": True}]
    outputs = [{"path": path, "sha256": digest(folder / path), "bytes": (folder / path).stat().st_size}
               for path in plan["steps"][0]["outputs"]]
    write_json(folder / "code_manifest.json", {"plan_hash": state["plan_hash"], "status": "succeeded", "scripts": scripts, "inputs": inputs})
    write_json(folder / "result_manifest.json", {"task_id": state["task_id"], "plan_hash": state["plan_hash"], "scripts": scripts, "inputs": inputs, "outputs": outputs})
    state["status"] = "succeeded"
    store.save(state)
    return TaskResearchAdapter(root, state["task_id"]), folder, store, state


def test_context_preserves_all_rows_na_and_no_guessed_selection(tmp_path):
    adapter, folder, _, _ = completed_task(tmp_path)
    before = {p: digest(p) for p in folder.rglob("*") if p.is_file()}
    context = adapter.load()
    assert context.disease == "colorectal cancer" and context.cohort_id == "cohort_example"
    assert len(context.evidence) == 3
    assert [r["observation"]["selected"] for r in context.evidence] == [None, None, None]
    assert context.evidence[0]["observation"]["direction"] == "lower_in_numerator"
    assert context.evidence[1]["observation"]["adjusted_p"] == 0.9
    assert context.evidence[2]["observation"]["effect"] is None
    assert "effect" in context.evidence[2]["observation"]["missing_fields"]
    assert context.evidence == adapter.load().evidence
    assert before == {p: digest(p) for p in folder.rglob("*") if p.is_file()}
    assert verify_context_sources(tmp_path, context)
    assert all(not Path(a["path"]).is_absolute() for a in context.artifacts)


def test_unknown_context_is_not_inferred_from_hcc_filename():
    spec = research_spec()
    spec["context"] = {"question": "Explain results", "contrast": {"numerator": "A", "denominator": "B"}}
    result = validate_research_plan(spec, ["tables/de.csv"], "patient")
    assert result["context"]["disease"] == result["context"]["species"] == "unknown"
    assert result["context"]["analysis_unit"] == "patient"


@pytest.mark.parametrize("defect", ["contrast", "unit", "undeclared", "duplicate", "path_traversal", "derived", "multimodal", "enrichment"])
def test_invalid_scientific_bindings_fail_before_execution(defect):
    spec = research_spec()
    output = spec["evidence_outputs"][0]
    if defect == "contrast": spec["context"].pop("contrast")
    elif defect == "unit": spec["context"]["analysis_unit"] = "injection"
    elif defect == "undeclared": output["path"] = "tables/unapproved.csv"
    elif defect == "duplicate": spec["evidence_outputs"].append(copy.deepcopy(output))
    elif defect == "path_traversal": output["path"] = "tables/../de.csv"
    elif defect == "derived": output["evidence_context"] = "derived_estimate"
    elif defect == "multimodal": output["measured_omics"].append("proteomics")
    else: output["layer"], output["entity_type"] = "pathway_enrichment", "pathway"
    with pytest.raises(ValueError):
        validate_research_plan(spec, ["tables/de.csv"], "patient")


@pytest.mark.parametrize("target", ["tables/de.csv", "reports/details.txt", "scripts/step_01.py", "input"])
def test_all_sources_verified_even_outputs_not_used_as_evidence(tmp_path, target):
    adapter, folder, _, _ = completed_task(tmp_path)
    path = tmp_path / "data/raw/metadata.csv" if target == "input" else folder / target
    path.write_text(path.read_text() + "tamper\n")
    with pytest.raises(ValueError, match="hash mismatch"):
        adapter.load()


@pytest.mark.parametrize("defect", ["approval", "state", "code_status", "output_missing", "script_mismatch", "manifest_plan"])
def test_approved_completion_and_manifests_are_required(tmp_path, defect):
    adapter, folder, store, state = completed_task(tmp_path)
    if defect in {"approval", "state"}:
        state["approved_hash" if defect == "approval" else "status"] = "changed"
        store.save(state)
    else:
        path = folder / ("code_manifest.json" if defect in {"code_status", "script_mismatch"} else "result_manifest.json")
        value = json.loads(path.read_text())
        if defect == "code_status": value["status"] = "failed"
        elif defect == "output_missing": value["outputs"].pop()
        elif defect == "script_mismatch": value["scripts"][0]["sha256"] = "0" * 64
        else: value["plan_hash"] = "0" * 64
        write_json(path, value)
    with pytest.raises(ValueError):
        adapter.load()


@pytest.mark.parametrize("table", [
    "gene,log2FC,p,padj,n\nA,1,0.1,0.1,3\nA,2,0.1,0.1,3\n",
    "gene,log2FC,p,padj,n\nA,1,1.1,0.1,3\n",
    "gene,log2FC,p,padj,n\nA,Inf,0.1,0.1,3\n",
    "gene,log2FC,p,padj,n\nA,1,0.1,0.1,3.5\n",
    "gene,log2FC,p,padj,n\nA,1,0.1\n",
    "gene,log2FC,p,padj,n\nNA,1,0.1,0.1,3\n",
])
def test_invalid_or_ambiguous_rows_are_not_silently_reinterpreted(tmp_path, table):
    adapter, _, _, _ = completed_task(tmp_path, table=table)
    with pytest.raises(ValueError):
        adapter.load()


def test_explicit_selection_requires_frozen_thresholds_and_unknown_scale_no_direction(tmp_path):
    spec = research_spec()
    spec["evidence_outputs"][0]["columns"]["selected"] = "selected"
    spec["evidence_outputs"][0]["columns"]["status"] = "status"
    spec["evidence_outputs"][0]["effect_scale"] = "unknown"
    spec["evidence_outputs"][0]["selection"] = {"adjusted_p_max": 0.05}
    adapter, _, _, _ = completed_task(tmp_path, research=spec, table="gene,log2FC,p,padj,n,selected,status\nA,-1,0.001,0.01,5,TRUE,tested\n")
    context = adapter.load()
    assert context.evidence[0]["observation"]["selected"] is True
    assert context.evidence[0]["observation"]["selection"]["verified"] is True
    assert context.evidence[0]["observation"]["direction"] == "unknown"
    assert build_context_evidence_graph(context)["edges"] == []


def test_selected_without_thresholds_is_never_promoted_to_significance(tmp_path):
    spec = research_spec()
    spec["evidence_outputs"][0]["columns"]["selected"] = "selected"
    adapter, _, _, _ = completed_task(tmp_path, research=spec,
                                      table="gene,log2FC,p,padj,n,selected\nA,-1,0.001,0.01,5,TRUE\n")
    context = adapter.load()
    observation = context.evidence[0]["observation"]
    assert observation["selected"] is None
    assert observation["selection"]["status"] == "not_verifiable"
    assert observation["selection"]["raw_selected"] is True
    assert "no_frozen_selection_thresholds" in observation["selection"]["reasons"]


def test_source_verifier_rejects_fabricated_observation(tmp_path):
    adapter, _, _, _ = completed_task(tmp_path)
    context = adapter.load()
    context.evidence[0]["observation"]["effect"] = 10
    with pytest.raises(ValueError, match="effect differs"):
        verify_context_sources(tmp_path, context)


def test_graph_keeps_null_negative_and_unknown_evidence(tmp_path):
    adapter, _, _, _ = completed_task(tmp_path)
    context = adapter.load()
    graph = build_context_evidence_graph(context)
    assert len(graph["evidence_records"]) == 3 and len(graph["edges"]) == 2
    assert graph["measured_omics"] == ["transcriptomics"]
    assert {e["type"] for e in graph["edges"]} == {"observed_association"}
    assert all(e["selected"] is None for e in graph["edges"])
    graph["evidence_records"][0]["observation"]["effect"] = 100
    assert context.evidence[0]["observation"]["effect"] == -1.2


def test_rna_pathways_are_derived_not_another_measured_modality(tmp_path):
    spec = research_spec()
    output = spec["evidence_outputs"][0]
    output.update(layer="pathway_enrichment", entity_type="pathway", effect_scale="NES",
                  measured_omics=[], evidence_context="derived_estimate", namespace="Reactome")
    adapter, _, _, _ = completed_task(tmp_path, research=spec)
    graph = build_context_evidence_graph(adapter.load())
    assert graph["measured_omics"] == []
    assert {e["type"] for e in graph["edges"]} == {"estimated_association"}


def test_entity_keys_separate_species_and_namespace(tmp_path):
    adapter, _, _, _ = completed_task(tmp_path)
    context = adapter.load()
    other = copy.deepcopy(context.evidence[0])
    other["evidence_id"] = "ev_" + "1" * 20
    other["biological_entity"]["species"] = "Mus musculus"
    context.evidence.append(other)
    graph = build_context_evidence_graph(context)
    assert len([n for n in graph["nodes"] if n["type"] == "gene"]) == 4


def test_bound_sample_and_pair_identifiers_are_private_local_fields(tmp_path):
    adapter, _, _, _ = completed_task(tmp_path, privacy=True)
    context = adapter.load()
    assert context.private_ids == ["TCGA-AA-0001", "TCGA-AA-0001-01A"]
    assert "TCGA-AA-0001" not in json.dumps(context.evidence)


def test_symlink_output_rejected_even_with_matching_hash(tmp_path):
    adapter, folder, _, _ = completed_task(tmp_path)
    target = folder / "reports/details.txt"
    duplicate = tmp_path / "duplicate.txt"
    duplicate.write_bytes(target.read_bytes())
    target.unlink()
    target.symlink_to(duplicate)
    with pytest.raises(ValueError, match="Symlink"):
        adapter.load()


def legacy_fixture(root):
    import pandas as pd
    from orchestrator.evidence_records import project_evidence_v2
    run = root / "workspace/runs/example"
    (run / "integration").mkdir(parents=True)
    (run / "transcriptomics/any_cohort").mkdir(parents=True)
    row = {"evidence_id": "ev_" + "a" * 20, "evidence_layer": "de", "omics": "transcriptomics",
           "feature_id": "GENE1", "cohort_id": "any_cohort", "direction": "down", "selected": False,
           "effect": -0.2, "pvalue": 0.5, "padj": 0.8, "n_pairs": 5,
           "artifact_path": "transcriptomics/any_cohort/de.csv", "row_key": "GENE1", "effect_scale": "log2_fold_change"}
    pd.DataFrame([row]).to_csv(run / "integration/evidence_registry.csv", index=False)
    pd.DataFrame([row]).to_csv(run / row["artifact_path"], index=False)
    record = project_evidence_v2(row)
    (run / "integration/evidence_registry.v2.jsonl").write_text(json.dumps(record) + "\n")
    write_json(run / "run.json", {"run_id": "example", "fingerprint": "a" * 64})
    paths = ["integration/evidence_registry.csv", "integration/evidence_registry.v2.jsonl", row["artifact_path"], "run.json"]
    write_json(run / "reasoning/frozen_sources.json", {"artifacts": [
        {"path": p, "sha256": digest(run / p), "bytes": (run / p).stat().st_size} for p in paths]})
    return run


def test_legacy_adapter_is_read_only_and_never_infers_hcc(tmp_path):
    run = legacy_fixture(tmp_path)
    before = {p: digest(p) for p in run.rglob("*") if p.is_file()}
    context = LegacyResearchAdapter(tmp_path, run, {}).load()
    assert context.disease == context.species == context.cohort_id == "unknown"
    assert context.evidence[0]["source"]["cohort"] == "any_cohort"
    assert context.evidence[0]["observation"]["selected"] is False
    assert context.evidence[0]["observation"]["direction"] == "unknown"
    assert before == {p: digest(p) for p in run.rglob("*") if p.is_file()}


def test_legacy_requires_original_source_freeze(tmp_path):
    run = legacy_fixture(tmp_path)
    (run / "transcriptomics/any_cohort/de.csv").write_text("changed")
    with pytest.raises(ValueError, match="Frozen reasoning source hash mismatch"):
        LegacyResearchAdapter(tmp_path, run, {}).load()


def test_legacy_rejects_incomplete_source_freeze(tmp_path):
    run = legacy_fixture(tmp_path)
    path = run / "reasoning/frozen_sources.json"
    payload = json.loads(path.read_text())
    payload["artifacts"] = [a for a in payload["artifacts"] if a["path"] != "transcriptomics/any_cohort/de.csv"]
    write_json(path, payload)
    with pytest.raises(ValueError, match="does not cover"):
        LegacyResearchAdapter(tmp_path, run, {}).load()


def selection_spec(**selection):
    spec = research_spec()
    output = spec["evidence_outputs"][0]
    output["columns"].update({"selected": "selected", "status": "status"})
    output["selection"] = {"adjusted_p_max": 0.05, **selection}
    return spec


def test_frozen_selection_accepts_consistent_rows(tmp_path):
    adapter, _, _, _ = completed_task(
        tmp_path, research=selection_spec(),
        table="gene,log2FC,p,padj,n,selected,status\n"
              "A,-1.2,0.001,0.01,20,TRUE,tested\n"
              "B,0.1,0.8,0.9,20,FALSE,tested\n")
    context = adapter.load()
    first, second = context.evidence
    assert first["observation"]["selected"] is True
    assert first["observation"]["selection"]["status"] == "verified_selected"
    assert second["observation"]["selected"] is False
    assert second["observation"]["selection"]["status"] == "verified_not_selected"


@pytest.mark.parametrize("table,error", [
    ("gene,log2FC,p,padj,n,selected,status\nA,-1,0.001,0.01,20,FALSE,tested\n",
     "disagrees with frozen"),
    ("gene,log2FC,p,padj,n,selected,status\nA,-1,0.5,0.5,20,TRUE,tested\n",
     "disagrees with frozen"),
    ("gene,log2FC,p,padj,n,selected,status\nA,-1,NA,NA,20,TRUE,not_estimable\n",
     "inconsistent with frozen"),
    ("gene,log2FC,p,padj,n,selected,status\nA,-1,0.001,0.01,2,TRUE,tested\n",
     "fewer_than_three"),
    ("gene,log2FC,p,padj,n,selected,status\nA,-1,0.001,0.0001,20,TRUE,tested\n",
     "adjusted_p is smaller"),
])
def test_inconsistent_frozen_selection_is_rejected(tmp_path, table, error):
    adapter, _, _, _ = completed_task(tmp_path, research=selection_spec(), table=table)
    with pytest.raises(ValueError, match=error):
        adapter.load()


def test_effect_threshold_and_analysis_unit_count_are_enforced(tmp_path):
    spec = selection_spec(effect_min=1.0, analysis_unit_count=20)
    adapter, _, _, _ = completed_task(
        tmp_path / "effect", research=spec,
        table="gene,log2FC,p,padj,n,selected,status\nA,0.5,0.001,0.01,20,TRUE,tested\n")
    with pytest.raises(ValueError, match="disagrees with frozen"):
        adapter.load()
    adapter, _, _, _ = completed_task(
        tmp_path / "units", research=selection_spec(analysis_unit_count=19),
        table="gene,log2FC,p,padj,n,selected,status\nA,1.2,0.001,0.01,20,TRUE,tested\n")
    with pytest.raises(ValueError, match="analysis_unit_count_mismatch"):
        adapter.load()


def test_require_selection_rejects_unfrozen_selected_column():
    spec = research_spec()
    spec["evidence_outputs"][0]["columns"]["selected"] = "selected"
    with pytest.raises(ValueError, match="selected column requires frozen"):
        validate_research_plan(spec, ["tables/de.csv"], "patient", require_selection=True)
    spec["evidence_outputs"][0]["selection"] = {"adjusted_p_max": 0.05}
    validate_research_plan(spec, ["tables/de.csv"], "patient", require_selection=True)


def test_selection_threshold_without_adjusted_p_binding_is_rejected():
    spec = research_spec()
    spec["evidence_outputs"][0]["columns"].pop("adjusted_p")
    spec["evidence_outputs"][0]["selection"] = {"adjusted_p_max": 0.05}
    with pytest.raises(ValueError, match="adjusted_p column"):
        validate_research_plan(spec, ["tables/de.csv"], "patient")


def test_source_verifier_rejects_reclassified_selection(tmp_path):
    adapter, _, _, _ = completed_task(tmp_path, research=selection_spec(),
                                      table="gene,log2FC,p,padj,n,selected,status\nA,-1.2,0.001,0.01,20,TRUE,tested\n")
    context = adapter.load()
    context.evidence[0]["observation"]["selected"] = False
    with pytest.raises(ValueError, match="selection selected differs"):
        verify_context_sources(tmp_path, context)


def test_selection_thresholds_must_come_from_user_or_config_parameters():
    from multiomics_tui.task_planner import _validate_selection_sources
    research = {"evidence_outputs": [{"selection": {"adjusted_p_max": 0.05}}]}
    with pytest.raises(ValueError, match="user/config"):
        _validate_selection_sources(research, [])
    _validate_selection_sources(research, [{"name": "fdr", "value": "0.05", "source": "user"}])
    _validate_selection_sources(research, [{"name": "fdr", "value": "0.05", "source": "config:analysis.de.padj"}])
    with pytest.raises(ValueError, match="user/config"):
        _validate_selection_sources(research, [{"name": "fdr", "value": "0.10", "source": "user"}])

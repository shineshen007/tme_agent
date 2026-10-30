"""Offline entrypoint contracts; no API, generated analysis or sandbox execution."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from multiomics_tui.controller import Session
from multiomics_tui.output_contracts import validate_research_contracts
from multiomics_tui.task_planner import generate_plan, validate_plan
from multiomics_tui.task_sessions import TaskStore, canonical_hash
from orchestrator.knowledge_context import knowledge_service
from test_research_context import completed_task, digest, research_spec


APP_ROOT = Path(__file__).resolve().parents[1]
CFG = {"runtime": {"workers": 1}, "llm": {"enabled": False, "provider": "fixture",
       "model": "offline", "base_url": "http://localhost:1234/v1", "auth_mode": "none"},
       "knowledge": {"shared_enabled": False}}


def raw_plan(root):
    source = root / "data/raw/input.csv"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("sample,value\nPRIVATE_SAMPLE,1\n", encoding="utf-8")
    return {"goal": "Explain synthetic GENE1 observations", "analysis_unit": "patient",
            "method": "descriptive fixture projection", "parameters": [],
            "inputs": ["data/raw/input.csv"], "limitations": ["Synthetic engineering fixture"],
            "steps": [{"id": "step_01", "language": "python", "purpose": "Fixture projection",
                       "outputs": ["tables/de.csv"]}], "research": research_spec()}


def approved_task(root):
    raw = raw_plan(root)
    plan = validate_plan(root, raw, raw["goal"], CFG)
    store = TaskStore(root)
    state = store.create(raw["goal"])
    state = store.save_plan(state["task_id"], plan)
    return store, store.approve(state["task_id"], state["plan_hash"])


def knowledge_payload(statement="GENE1 may reflect altered cell composition."):
    text = "Synthetic fixture: GENE1 expression accompanies altered cell composition."
    return {"title": "GENE1 mechanism fixture", "kind": "biological_inference", "statement": statement,
            "context": {"species": "Homo sapiens"}, "sources": [{"source_id": "fixture:1",
            "source_type": "generated_report", "locator": "reports/fixture.md", "primary": False,
            "text": text, "sha256": hashlib.sha256(text.encode()).hexdigest()}],
            "entities": [{"id": "gene:9606:GENE1", "type": "gene", "label": "GENE1"}], "relations": []}


def reviewed_entry(root, statement=None):
    service = knowledge_service(root, CFG["knowledge"], read_only=False)
    proposal = service.propose(knowledge_payload(statement) if statement else knowledge_payload())
    entry = service.review(proposal["id"], proposal["sha256"], "approve")
    return service, entry


@pytest.mark.parametrize("mutation", ["columns", "contrast", "effect_scale"])
def test_research_mapping_is_bound_to_approval_hash(tmp_path, mutation):
    store, state = approved_task(tmp_path)
    task_id = state["task_id"]
    approved = store.plan(task_id)
    assert state["approved_hash"] == canonical_hash(approved)
    assert approved["research"]["evidence_outputs"][0]["columns"]["effect"] == "log2FC"
    tampered = deepcopy(approved)
    spec = tampered["research"]["evidence_outputs"][0]
    if mutation == "columns":
        spec["columns"]["effect"] = "another_effect"
    elif mutation == "effect_scale":
        spec["effect_scale"] = "difference"
    else:
        tampered["research"]["context"]["contrast"] = {"numerator": "adjacent", "denominator": "tumor"}
    assert canonical_hash(tampered) != state["approved_hash"]
    (store.directory(task_id) / "plan_v001.json").write_text(json.dumps(tampered))
    with pytest.raises(ValueError, match="Plan hash mismatch"):
        store.plan(task_id)
    assert store.get(task_id)["approved_hash"] == state["approved_hash"]


def test_planner_rejects_unbound_output_and_changed_knowledge_hash(tmp_path):
    raw = raw_plan(tmp_path)
    raw["research"]["evidence_outputs"][0]["path"] = "tables/undeclared.csv"
    with pytest.raises(ValueError, match="declared"):
        validate_plan(tmp_path, raw, raw["goal"], CFG)
    raw = raw_plan(tmp_path)
    raw["knowledge_snapshot"] = {"content": {"entries": []}, "sha256": "0" * 64}
    with pytest.raises(ValueError, match="snapshot hash"):
        validate_plan(tmp_path, raw, raw["goal"], CFG)


def test_generate_plan_freezes_local_reviewed_knowledge_not_model_injection(tmp_path, monkeypatch):
    from multiomics_tui import input_inspection, r_env
    raw = raw_plan(tmp_path)
    service, entry = reviewed_entry(tmp_path)
    service.propose(knowledge_payload("GENE1 PENDING_ONLY candidate should never reach the planner."))
    inspection_calls, model_calls = [], []

    def inspect(root, inputs, cfg):
        inspection_calls.append(deepcopy(inputs))
        audit = root / ".runtime/input_inspections/fixture.json"
        audit.parent.mkdir(parents=True)
        local = {"profiles": [{**inputs[0], "object_class": "data.frame", "keys": {}, "columns": {}}],
                 "software": {}, "apis": {}}
        audit.write_text(json.dumps(local))
        return {**local, "audit_path": str(audit.relative_to(root))}

    def chat(role, system, user, schema_name, validator):
        model_calls.append((role, system, schema_name))
        if role == "tui_task_plan_inputs":
            result = {"inputs": raw["inputs"]}
        else:
            injected = {"entries": [{"statement": "MODEL_INJECTED_KNOWLEDGE"}]}
            result = {**deepcopy(raw), "input_bindings": {"inventory": True},
                      "knowledge_snapshot": {"content": injected, "sha256": canonical_hash(injected)}}
        validator(result)
        return {"parsed": result}

    monkeypatch.setattr(input_inspection, "inspect_inputs", inspect)
    monkeypatch.setattr(r_env, "availability_note", lambda cfg: "")
    checked = generate_plan(SimpleNamespace(chat_json=chat), tmp_path, raw["goal"], CFG)
    snapshot = checked["knowledge_snapshot"]
    assert snapshot["sha256"] == canonical_hash(snapshot["content"])
    assert entry["entry_id"] in json.dumps(snapshot)
    assert "MODEL_INJECTED_KNOWLEDGE" not in json.dumps(snapshot)
    assert "PENDING_ONLY" not in model_calls[-1][1]
    assert entry["statement"] in model_calls[-1][1]
    assert "untrusted source content" in model_calls[-1][1]
    assert model_calls[-1][2] == "tui_task_plan_v7"
    assert inspection_calls[0][0]["sha256"] == digest(tmp_path / raw["inputs"][0])
    store = TaskStore(tmp_path)
    state = store.save_plan(store.create(raw["goal"])["task_id"], checked)
    assert store.approve(state["task_id"], state["plan_hash"])["approved_hash"] == canonical_hash(checked)


@pytest.mark.parametrize("bad_contract", [
    [],
    [{"path": "tables/de.csv", "format": "csv", "columns": ["gene", "log2FC", "p", "n"]}],
    [{"path": "tables/de.csv", "format": "json", "columns": ["gene", "log2FC", "p", "padj", "n"]}],
])
def test_output_contract_must_cover_every_frozen_evidence_column(bad_contract):
    with pytest.raises(ValueError, match="frozen research evidence mapping"):
        validate_research_contracts({"research": research_spec()}, bad_contract, {"outputs": ["tables/de.csv"]})


def test_output_contract_also_binds_row_key_and_only_relevant_step():
    spec = research_spec()
    spec["evidence_outputs"][0]["row_key"] = ["gene", "contrast_id"]
    contract = {"path": "tables/de.csv", "format": "csv", "columns": ["gene", "log2FC", "p", "padj", "n"]}
    with pytest.raises(ValueError, match="frozen research evidence mapping"):
        validate_research_contracts({"research": spec}, [contract], {"outputs": ["tables/de.csv"]})
    contract["columns"] += ["contrast_id", "extra_annotation"]
    validate_research_contracts({"research": spec}, [contract], {"outputs": ["tables/de.csv"]})
    validate_research_contracts({"research": spec}, [], {"outputs": ["reports/later.txt"]})


@pytest.mark.parametrize("analysis_fails,research_fails", [(False, False), (False, True), (True, False)])
def test_runner_research_hook_only_after_verified_analysis(tmp_path, monkeypatch, analysis_fails, research_fails):
    from multiomics_tui import llm, task_runner
    from orchestrator import research_pipeline
    store, state = approved_task(tmp_path)
    task_id = state["task_id"]
    folder = tmp_path / "analysis" / task_id
    calls = []
    contract = {"path": "tables/de.csv", "format": "csv", "columns": ["gene", "log2FC", "p", "padj", "n"]}

    def codegen(client, plan, on_step, **kwargs):
        on_step(plan["steps"][0], "# Offline fixture; never executed\n", [contract])
        return {}

    def subprocess_fixture(argv, **kwargs):
        if any(Path(str(arg)).name == "task_guard.py" for arg in argv):
            calls.append("analysis")
            assert store.get(task_id)["status"] == "running"
            if not analysis_fails:
                (folder / "tables/de.csv").write_text("gene,log2FC,p,padj,n\nGENE1,-1,0.01,0.02,20\nGENE2,0.1,0.8,0.9,20\n")
            return SimpleNamespace(returncode=1 if analysis_fails else 0)
        assert argv == ["fixture-rscript", "--vanilla", "-e", "cat(R.version.string)"]
        return SimpleNamespace(returncode=0, stdout="R fixture (not executed)")

    def research(root, requested_task, **kwargs):
        calls.append("research")
        assert requested_task == task_id and store.get(task_id)["status"] == "succeeded"
        assert json.loads((folder / "code_manifest.json").read_text())["status"] == "succeeded"
        manifest = json.loads((folder / "result_manifest.json").read_text())
        assert manifest["outputs"][0]["sha256"] == digest(folder / "tables/de.csv")
        if research_fails:
            raise RuntimeError("Injected research failure")
        return {"research_status": "partial", "attempt": "offline-fixture"}

    monkeypatch.setattr(llm, "load_tui_config", lambda *args, **kwargs: deepcopy(CFG))
    monkeypatch.setattr(task_runner, "guard_available", lambda: True)
    monkeypatch.setattr(task_runner, "_step_environment", lambda *args: {})
    monkeypatch.setattr(task_runner, "_check_code", lambda *args, **kwargs: None)
    monkeypatch.setattr(task_runner, "_codegen", codegen)
    monkeypatch.setattr(task_runner, "rscript_path", lambda root: "fixture-rscript")
    monkeypatch.setattr(task_runner.subprocess, "run", subprocess_fixture)
    monkeypatch.setattr(research_pipeline, "run_task_research", research)
    if analysis_fails:
        with pytest.raises(RuntimeError, match="Step step_01 failed"):
            task_runner.run(tmp_path, task_id, client=object())
        assert calls == ["analysis"] and store.get(task_id)["status"] == "failed"
        assert not (folder / "result_manifest.json").exists()
    else:
        assert task_runner.run(tmp_path, task_id, client=object()) == 0
        assert calls == ["analysis", "research"] and store.get(task_id)["status"] == "succeeded"
        assert (folder / "reports/task_report.md").is_file()
        if research_fails:
            assert store.get(task_id)["research_status"] == "blocked"
            assert store.get(task_id)["scientific_status"] == "NOT_EVALUABLE"
            assert "Injected research failure" in store.get(task_id)["research_error"]


def test_controller_research_retry_uses_research_worker_and_preserves_analysis(tmp_path, monkeypatch):
    from multiomics_tui import model_config
    _, folder, store, state = completed_task(tmp_path)
    (store.directory(state["task_id"]) / "model_config.json").write_text(json.dumps(CFG["llm"]))
    session = Session(tmp_path, cfg=deepcopy(CFG), allow_llm=False, app_root=APP_ROOT)
    before = {str(p.relative_to(folder)): digest(p) for p in folder.rglob("*") if p.is_file()}
    start = Mock(return_value={"job_id": "research-fixture", "status": "running"})
    monkeypatch.setattr(session.jobs, "active_pipeline", lambda: None)
    monkeypatch.setattr(session.jobs, "start", start)
    monkeypatch.setattr(model_config, "worker_environment", lambda cfg: {})
    result = session.handle_command("/research " + state["task_id"])
    assert result.kind == "job" and start.call_count == 1
    args, kwargs = start.call_args
    assert args[0] == "task"
    assert args[1] == [sys.executable, str(APP_ROOT / "orchestrator/research_pipeline.py"), str(tmp_path.resolve()), state["task_id"]]
    assert kwargs["run_id"] == state["task_id"]
    assert store.get(state["task_id"])["status"] == "succeeded"
    assert before == {str(p.relative_to(folder)): digest(p) for p in folder.rglob("*") if p.is_file()}
    # Verify the real script import path from an unrelated cwd, without starting research.
    help_result = subprocess.run([args[1][0], args[1][1], "--help"], cwd=tmp_path,
                                 capture_output=True, text=True, timeout=30)
    assert help_result.returncode == 0, help_result.stderr
    assert "task_id" in help_result.stdout


@pytest.mark.parametrize("invalid", ["analysis_failed", "approval_changed", "plan_changed", "busy", "provider_changed"])
def test_controller_research_refuses_unapproved_or_changed_task(tmp_path, monkeypatch, invalid):
    _, _, store, state = completed_task(tmp_path)
    task_id = state["task_id"]
    session = Session(tmp_path, cfg=deepcopy(CFG), allow_llm=False, app_root=APP_ROOT)
    start = Mock()
    monkeypatch.setattr(session.jobs, "start", start)
    monkeypatch.setattr(session.jobs, "active_pipeline", lambda: {"job_id": "busy"} if invalid == "busy" else None)
    if invalid == "analysis_failed":
        state["status"] = "failed"
        store.save(state)
    elif invalid == "approval_changed":
        state["approved_hash"] = "0" * 64
        store.save(state)
    elif invalid == "plan_changed":
        path = store.directory(task_id) / "plan_v001.json"
        changed = json.loads(path.read_text())
        changed["research"]["context"]["question"] = "A different research question"
        path.write_text(json.dumps(changed))
    elif invalid == "provider_changed":
        frozen = {**CFG["llm"], "base_url": "https://different-provider.invalid/v1"}
        (store.directory(task_id) / "model_config.json").write_text(json.dumps(frozen))
    result = session.handle_command("/research " + task_id)
    assert result.kind == "text" and not start.called


def test_controller_wiki_requires_two_separate_content_bound_confirmations(tmp_path):
    session = Session(tmp_path, cfg=deepcopy(CFG), allow_llm=False, app_root=APP_ROOT)
    resource = tmp_path / "knowledge/resources/fixture.json"
    resource.parent.mkdir(parents=True)
    resource.write_text(json.dumps(knowledge_payload()))
    reader = knowledge_service(tmp_path, CFG["knowledge"])
    preview = session.handle_command("/wiki import knowledge/resources/fixture.json")
    assert preview.kind == "need_confirm" and reader.pending() == []
    session.confirm("wrong-confirmation-id")
    assert reader.pending() == []
    session.confirm(preview.pending_id)
    proposals = reader.pending()
    assert len(proposals) == 1 and reader.search("GENE1") == []
    session.confirm(preview.pending_id)
    assert len(reader.pending()) == 1
    review = session.handle_command("/wiki review " + proposals[0]["id"] + " approve")
    assert review.kind == "need_confirm" and reader.search("GENE1") == []
    session.confirm(review.pending_id)
    entries = reader.search("GENE1")
    assert len(entries) == 1 and entries[0]["kind"] == "biological_inference"
    assert reader.pending() == []


def test_controller_wiki_rejects_resource_changed_after_confirmation_preview(tmp_path):
    session = Session(tmp_path, cfg=deepcopy(CFG), allow_llm=False, app_root=APP_ROOT)
    resource = tmp_path / "knowledge/resources/fixture.json"
    resource.parent.mkdir(parents=True)
    resource.write_text(json.dumps(knowledge_payload()))
    preview = session.handle_command("/wiki import knowledge/resources/fixture.json")
    assert preview.kind == "need_confirm"
    resource.write_text(json.dumps(knowledge_payload("A changed GENE1 claim.")))
    session.confirm(preview.pending_id)
    assert knowledge_service(tmp_path, CFG["knowledge"]).pending() == []
    assert session.pending is None


def test_ask_reads_only_reviewed_knowledge_and_retraction_removes_it(tmp_path):
    service, entry = reviewed_entry(tmp_path)
    service.propose(knowledge_payload("GENE1 PENDING_ONLY unreviewed candidate."))
    session = Session(tmp_path, cfg=deepcopy(CFG), allow_llm=False, app_root=APP_ROOT)
    wiki = tmp_path / "knowledge/wiki"
    before = {str(p.relative_to(wiki)): digest(p) for p in wiki.rglob("*") if p.is_file()}
    response = session.handle_command("/ask GENE1")
    assert response.kind == "excerpts" and response.mode == "retrieval"
    assert len(response.excerpts) == 1
    assert entry["entry_id"] in response.excerpts[0]["source"]
    assert "not independent validation" in response.excerpts[0]["text"]
    assert "PENDING_ONLY" not in json.dumps(response.excerpts)
    assert before == {str(p.relative_to(wiki)): digest(p) for p in wiki.rglob("*") if p.is_file()}
    retraction = session.handle_command("/wiki retract " + entry["entry_id"])
    assert retraction.kind == "need_confirm"
    assert session.handle_command("/ask GENE1").excerpts
    session.confirm(retraction.pending_id)
    assert session.handle_command("/ask GENE1").excerpts == []
    assert service.get(entry["entry_id"])["status"] == "retracted"


def test_ask_honors_selected_shared_library_and_disable_setting(tmp_path):
    settings = {"shared_root": str(tmp_path / "selected_shared")}
    publisher = knowledge_service(tmp_path / "publisher", settings, read_only=False)
    value = knowledge_payload("GENE1 reviewed shared context remains an inference.")
    value['sources'][0].update(source_type='publication', locator='https://example.org/fixture',
                               primary=True, quote='GENE1 expression accompanies altered cell composition.',
                               metadata={'content_scope': 'abstract'})
    value["shareable"] = True
    proposal = publisher.propose(value)
    entry = publisher.review(proposal["id"], proposal["sha256"], "approve")
    publisher.publish(entry["entry_id"], entry["sha256"], confirmed=True)
    root = tmp_path / "consumer"
    cfg = {**deepcopy(CFG), "knowledge": settings}
    session = Session(root, cfg=cfg, allow_llm=False, app_root=APP_ROOT)
    excerpts = session.handle_command("/ask GENE1").excerpts
    assert len(excerpts) == 1 and excerpts[0]["source"].startswith("wiki:shared:")
    session.cfg["knowledge"] = {**settings, "shared_enabled": False}
    assert session.handle_command("/ask GENE1").excerpts == []
    assert not (root / "knowledge/wiki").exists()

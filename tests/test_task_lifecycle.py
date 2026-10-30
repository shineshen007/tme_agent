"""A05: a failed post-approval start must not leave a dead approved task."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from multiomics_tui.controller import Session
from multiomics_tui.task_planner import validate_plan
from multiomics_tui.task_sessions import TaskStore


def make_session(root: Path, cfg: dict) -> tuple[Session, TaskStore, dict]:
    source = root / "data/raw/input.csv"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("sample,value\na,1\n", encoding="utf-8")
    request = "Synthetic start recovery with data/raw/input.csv"
    raw = {"goal": request, "analysis_unit": "input row", "method": "descriptive inventory",
           "parameters": [], "inputs": ["data/raw/input.csv"],
           "steps": [{"id": "step_00", "language": "python", "purpose": "fixture",
                      "outputs": ["tables/out.csv"]}],
           "limitations": ["Synthetic A05 fixture"]}
    store = TaskStore(root)
    state = store.create(request)
    plan = validate_plan(root, raw, request, cfg)
    state = store.save_plan(state["task_id"], plan)
    session = Session(root, cfg=cfg, allow_llm=False)
    session.active_task = state["task_id"]
    session.pending = {"id": "pending", "action": "task_plan", "task_id": state["task_id"],
                       "plan_hash": state["plan_hash"], "ready": True}
    return session, store, state


def prepare(session, monkeypatch):
    from multiomics_tui import model_config
    monkeypatch.setattr(model_config, "worker_environment", lambda cfg: {})
    monkeypatch.setattr(session.jobs, "active_pipeline", lambda: None)


@pytest.mark.parametrize("failure", [ValueError("A TUI job is already running or stopping"),
                                     OSError("fixture process launch failure")])
def test_admission_and_process_start_failures_block_the_task(project_root, stub_cfg, monkeypatch, failure):
    session, store, state = make_session(project_root, stub_cfg)
    prepare(session, monkeypatch)
    monkeypatch.setattr(session.jobs, "start", lambda *a, **k: (_ for _ in ()).throw(failure))
    response = session.confirm("pending")
    assert response.kind == "text" and "could not start" in response.message
    current = store.get(state["task_id"])
    assert current["status"] == "blocked" and current["job_id"] is None
    assert current["failure_stage"] == "start" and type(failure).__name__ in current["error"]
    assert current["approved_hash"] == state["plan_hash"]
    assert store.reconcile(state["task_id"], session.jobs)["status"] == "blocked"
    approve_again = session.handle(f"/approve {state['task_id']}")
    assert approve_again.kind == "text" and "no plan awaits approval" in approve_again.message


def test_model_snapshot_failure_blocks_the_task(project_root, stub_cfg, monkeypatch):
    session, store, state = make_session(project_root, stub_cfg)
    prepare(session, monkeypatch)
    from multiomics_tui import jobs
    real_atomic_json = jobs._atomic_json

    def fail_snapshot(path, payload):
        if str(path).endswith("model_config.json"):
            raise OSError("fixture snapshot failure")
        return real_atomic_json(path, payload)

    monkeypatch.setattr(jobs, "_atomic_json", fail_snapshot)
    start = []
    monkeypatch.setattr(session.jobs, "start", lambda *a, **k: start.append(k) or {})
    response = session.confirm("pending")
    assert response.kind == "text" and "could not start" in response.message
    current = store.get(state["task_id"])
    assert current["status"] == "blocked" and current["job_id"] is None
    assert start == []


def test_retry_after_start_failure_replans_without_starting_a_job(project_root, stub_cfg, monkeypatch):
    session, store, state = make_session(project_root, stub_cfg)
    prepare(session, monkeypatch)
    monkeypatch.setattr(session.jobs, "start", lambda *a, **k: (_ for _ in ()).throw(ValueError("denied")))
    assert session.confirm("pending").kind == "text"
    assert store.get(state["task_id"])["status"] == "blocked"
    from multiomics_tui import controller
    session.allow_llm = True
    session.client = object()
    raw = {"goal": "Synthetic retry plan", "analysis_unit": "input row",
           "method": "descriptive inventory", "parameters": [],
           "inputs": ["data/raw/input.csv"],
           "steps": [{"id": "step_00", "language": "python", "purpose": "fixture",
                      "outputs": ["tables/out.csv"]}],
           "limitations": ["Synthetic retry fixture"]}
    monkeypatch.setattr(controller, "generate_plan", lambda *a, **k: validate_plan(
        project_root, raw, store.user_text(state["task_id"]), stub_cfg))
    retry = session.handle(f"/retry {state['task_id']}")
    assert retry.kind == "need_confirm"
    after_retry = store.get(state["task_id"])
    assert after_retry["status"] == "awaiting_approval"
    assert after_retry["plan_version"] == 2 and after_retry["approved_hash"] is None
    calls = []
    monkeypatch.setattr(session.jobs, "start", lambda *a, **k: calls.append(k) or {
        "job_id": "job_fixture", "status": "running", "log_path": "workspace/tui/logs/job_fixture.log"})
    confirmed = session.confirm(retry.pending_id)
    assert confirmed.kind == "job" and len(calls) == 1
    assert store.get(state["task_id"])["job_id"] == "job_fixture"


def test_start_exception_that_already_created_a_job_binds_it(project_root, stub_cfg, monkeypatch):
    session, store, state = make_session(project_root, stub_cfg)
    from multiomics_tui import model_config
    monkeypatch.setattr(model_config, "worker_environment", lambda cfg: {})
    calls = iter([None, {"job_id": "job_existing", "run_id": state["task_id"],
                         "log_path": "workspace/tui/logs/job_existing.log", "status": "running"}])
    monkeypatch.setattr(session.jobs, "active_pipeline", lambda: next(calls, None))
    monkeypatch.setattr(session.jobs, "start", lambda *a, **k: (_ for _ in ()).throw(OSError("raised after submit")))
    response = session.confirm("pending")
    assert response.kind == "job" and response.job["job_id"] == "job_existing"
    assert store.get(state["task_id"])["job_id"] == "job_existing"

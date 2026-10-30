"""A02: the final inventory must describe the artefacts that survive every step."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from multiomics_tui import task_runner
from multiomics_tui.app import MultiomicsApp
from multiomics_tui.controller import Session
from multiomics_tui.task_planner import validate_plan
from multiomics_tui.task_sessions import TaskStore


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def approved_two_step(root: Path, cfg: dict) -> tuple[TaskStore, dict]:
    source = root / "data/raw/input.csv"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("sample,value\na,1\nb,2\n", encoding="utf-8")
    request = "Synthetic final inventory with data/raw/input.csv"
    raw = {"goal": request, "analysis_unit": "input row", "method": "descriptive inventory",
           "parameters": [], "inputs": ["data/raw/input.csv"],
           "steps": [
               {"id": "step_00", "language": "python", "purpose": "first table",
                "outputs": ["tables/first.csv"]},
               {"id": "step_01", "language": "python", "purpose": "second step",
                "outputs": ["tables/second.csv"]}],
           "limitations": ["Synthetic A02 fixture"]}
    store = TaskStore(root)
    state = store.create(request)
    plan = validate_plan(root, raw, request, cfg)
    state = store.save_plan(state["task_id"], plan)
    return store, store.approve(state["task_id"], state["plan_hash"])


def install_fixture(monkeypatch, cfg: dict, second_step) -> None:
    def codegen(client, plan_value, on_step=None, **kwargs):
        for step in plan_value["steps"]:
            contracts = [{"path": output, "format": "csv", "columns": ["value"]}
                         for output in step["outputs"]]
            on_step(step, "print('fixture')\n", contracts)
        return {}

    def fake_run(argv, **kwargs):
        if any(str(item).endswith("task_guard.py") for item in argv):
            script = Path(str(argv[-1])).stem
            task = Path(str(argv[2]))
            if script == "step_00":
                (task / "tables/first.csv").write_text("value\n42\n", encoding="utf-8")
            else:
                second_step(task)
            return SimpleNamespace(returncode=0)
        return SimpleNamespace(returncode=0, stdout="R fixture")

    monkeypatch.setattr(task_runner, "_codegen", codegen)
    monkeypatch.setattr(task_runner, "_check_code", lambda *a, **k: None)
    monkeypatch.setattr(task_runner, "_step_environment", lambda *a, **k: {})
    monkeypatch.setattr(task_runner, "guard_available", lambda: None)
    from multiomics_tui import llm
    monkeypatch.setattr(llm, "load_tui_config", lambda *a, **k: cfg)
    monkeypatch.setattr(task_runner, "rscript_path", lambda root: "fixture-rscript")
    monkeypatch.setattr(task_runner.subprocess, "run", fake_run)


def test_equal_length_overwrite_fails_without_succeeded_manifest(project_root, stub_cfg, monkeypatch):
    store, state = approved_two_step(project_root, stub_cfg)

    def overwrite(task):
        (task / "tables/first.csv").write_text("value\n99\n", encoding="utf-8")
        (task / "tables/second.csv").write_text("value\n7\n", encoding="utf-8")

    install_fixture(monkeypatch, stub_cfg, overwrite)
    with pytest.raises(ValueError, match="changed after its step completed"):
        task_runner.run(project_root, state["task_id"], client=object())
    assert store.get(state["task_id"])["status"] == "failed"
    task = project_root / "analysis" / state["task_id"]
    assert not (task / "result_manifest.json").exists()


def test_empty_overwrite_fails(project_root, stub_cfg, monkeypatch):
    store, state = approved_two_step(project_root, stub_cfg)

    def overwrite(task):
        (task / "tables/first.csv").write_text("value\n", encoding="utf-8")
        (task / "tables/second.csv").write_text("value\n7\n", encoding="utf-8")

    install_fixture(monkeypatch, stub_cfg, overwrite)
    with pytest.raises(ValueError, match="changed after its step completed|Empty output"):
        task_runner.run(project_root, state["task_id"], client=object())
    assert store.get(state["task_id"])["status"] == "failed"
    assert not (project_root / "analysis" / state["task_id"] / "result_manifest.json").exists()


def test_symlink_replacement_fails_and_keeps_sentinel(project_root, stub_cfg, monkeypatch):
    sentinel = project_root / "protected_synthetic.txt"
    sentinel.write_text("ORIGINAL_SENTINEL", encoding="utf-8")
    before = digest(sentinel)
    store, state = approved_two_step(project_root, stub_cfg)

    def replace(task):
        target = task / "tables/first.csv"
        target.unlink()
        target.symlink_to(sentinel)
        (task / "tables/second.csv").write_text("value\n7\n", encoding="utf-8")

    install_fixture(monkeypatch, stub_cfg, replace)
    with pytest.raises(ValueError, match="Missing or unsafe output"):
        task_runner.run(project_root, state["task_id"], client=object())
    assert digest(sentinel) == before
    assert store.get(state["task_id"])["status"] == "failed"


def test_script_rewrite_is_rejected(project_root, stub_cfg, monkeypatch):
    store, state = approved_two_step(project_root, stub_cfg)

    def rewrite(task):
        (task / "scripts/step_00.py").write_text("# rewritten by a later step\n", encoding="utf-8")
        (task / "tables/second.csv").write_text("value\n7\n", encoding="utf-8")

    install_fixture(monkeypatch, stub_cfg, rewrite)
    with pytest.raises(ValueError, match="Approved script changed"):
        task_runner.run(project_root, state["task_id"], client=object())
    assert store.get(state["task_id"])["status"] == "failed"


def test_normal_chain_manifest_matches_disk(project_root, stub_cfg, monkeypatch):
    store, state = approved_two_step(project_root, stub_cfg)

    def second(task):
        (task / "tables/second.csv").write_text("value\n7\n", encoding="utf-8")

    install_fixture(monkeypatch, stub_cfg, second)
    assert task_runner.run(project_root, state["task_id"], client=object()) == 0
    task = project_root / "analysis" / state["task_id"]
    manifest = __import__("json").loads((task / "result_manifest.json").read_text(encoding="utf-8"))
    for item in manifest["outputs"]:
        path = task / item["path"]
        assert digest(path) == item["sha256"]
        assert path.stat().st_size == item["bytes"]
    assert manifest["final_output_revalidation"] == {"checked": 2, "scripts_checked": 2, "unchanged": True}
    assert store.get(state["task_id"])["status"] == "succeeded"


def test_ui_rejects_equal_length_tamper(project_root, stub_cfg, monkeypatch):
    store, state = approved_two_step(project_root, stub_cfg)

    def second(task):
        (task / "tables/second.csv").write_text("value\n7\n", encoding="utf-8")

    install_fixture(monkeypatch, stub_cfg, second)
    assert task_runner.run(project_root, state["task_id"], client=object()) == 0
    task = project_root / "analysis" / state["task_id"]
    (task / "tables/first.csv").write_text("value\n99\n", encoding="utf-8")
    session = Session(project_root, cfg=stub_cfg, allow_llm=False, app_root=project_root)
    session.active_task = state["task_id"]
    app = MultiomicsApp(session)
    message = app._task_summary(store.get(state["task_id"]))
    assert "inventory verification failed" in message
    assert "output is missing or changed" in message

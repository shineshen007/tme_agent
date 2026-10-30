"""A01: trusted-parent writes must not follow links planted by generated code.

The real macOS/OS guard is exercised; only the machine doctor record is replaced by
the shared autouse fixture. Synthetic sentinels stand in for protected data.
"""
from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path

import pytest

from multiomics_tui import task_runner
from multiomics_tui.safe_fs import open_beneath
from multiomics_tui.task_sessions import TaskStore
from multiomics_tui.task_planner import validate_plan


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def approved_plan(root: Path, steps: list[dict]) -> tuple[TaskStore, dict]:
    source = root / "data/raw/input.csv"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("sample,value\na,1\nb,2\n", encoding="utf-8")
    request = "Synthetic parent write boundary with data/raw/input.csv"
    raw = {"goal": request, "analysis_unit": "input row", "method": "descriptive inventory",
           "parameters": [], "inputs": ["data/raw/input.csv"],
           "steps": steps, "limitations": ["Synthetic A01 fixture"]}
    store = TaskStore(root)
    state = store.create(request)
    plan = validate_plan(root, raw, request, {"runtime": {}, "llm": {"enabled": False}})
    state = store.save_plan(state["task_id"], plan)
    return store, store.approve(state["task_id"], state["plan_hash"])


def install_codegen(monkeypatch, payload: dict) -> None:
    def codegen(client, plan_value, on_step=None, **kwargs):
        for step in plan_value["steps"]:
            contracts = [{"path": output, "format": "csv", "columns": ["value"]}
                         for output in step["outputs"]]
            on_step(step, payload[step["id"]], contracts)
        return {}
    monkeypatch.setattr(task_runner, "_codegen", codegen)


def test_failure_report_refuses_file_link(tmp_path):
    root = tmp_path.resolve() / "project"
    root.mkdir()
    (root / "config").mkdir()
    (root / "config/study_config.yaml").write_text("schema_version: '1.0'\n", encoding="utf-8")
    sentinel = root / "protected_synthetic.txt"
    sentinel.write_text("ORIGINAL_SENTINEL", encoding="utf-8")
    before = digest(sentinel)
    store = TaskStore(root)
    state = store.create("A01 failure report link")
    state.update(status="running", current_stage="execution", current_step="step_00")
    store.save(state)
    task = root / "analysis" / state["task_id"]
    (task / "reports").mkdir(parents=True)
    (task / "reports/failure.txt").symlink_to(sentinel)
    try:
        raise RuntimeError("SYNTHETIC_FAILURE_MARKER")
    except RuntimeError as exc:
        task_runner._record_failure(root, state["task_id"], exc)
    assert digest(sentinel) == before
    after = store.get(state["task_id"])
    assert after["status"] == "failed" and after["failure_stage"] == "execution"
    assert "SYNTHETIC_FAILURE_MARKER" not in sentinel.read_text()


def test_record_failure_refuses_reports_directory_link(tmp_path):
    root = tmp_path.resolve() / "project"
    root.mkdir()
    (root / "config").mkdir()
    (root / "config/study_config.yaml").write_text("schema_version: '1.0'\n", encoding="utf-8")
    external = root / "external"
    external.mkdir()
    sentinel = external / "sentinel.txt"
    sentinel.write_text("ORIGINAL_SENTINEL", encoding="utf-8")
    before = digest(sentinel)
    store = TaskStore(root)
    state = store.create("A01 reports ancestor link")
    state.update(status="running", current_stage="execution", current_step="step_00")
    store.save(state)
    task = root / "analysis" / state["task_id"]
    task.mkdir(parents=True)
    (task / "reports").symlink_to(external)
    try:
        raise RuntimeError("SYNTHETIC_FAILURE_MARKER")
    except RuntimeError as exc:
        task_runner._record_failure(root, state["task_id"], exc)
    assert digest(sentinel) == before
    assert not (external / "failure.txt").exists()
    assert store.get(state["task_id"])["status"] == "failed"


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor-relative race probe")
def test_open_beneath_does_not_follow_swapped_ancestor_link(tmp_path):
    base = tmp_path.resolve() / "task"
    base.mkdir()
    real = base / "current"
    spare = base / "spare"
    external = tmp_path.resolve() / "external"
    external.mkdir()
    sentinel = external / "sentinel.txt"
    sentinel.write_text("ORIGINAL_SENTINEL", encoding="utf-8")
    real.mkdir()
    spare.symlink_to(external)
    stop = threading.Event()

    def toggle():
        while not stop.is_set():
            try:
                current, other = real, spare
                real.rename(base / "swap")
                other.rename(real)
                (base / "swap").rename(spare)
            except OSError:
                continue

    thread = threading.Thread(target=toggle, daemon=True)
    thread.start()
    try:
        for _ in range(300):
            try:
                fd = open_beneath(base, "current/out.txt", os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write("inside")
            except (OSError, ValueError):
                pass
    finally:
        stop.set()
        thread.join(timeout=5)
    assert sentinel.read_text(encoding="utf-8") == "ORIGINAL_SENTINEL"
    assert not (external / "out.txt").exists()


def test_step_log_refuses_link_and_next_step_does_not_run(project_root, monkeypatch):
    sentinel = project_root / "protected_synthetic.txt"
    sentinel.write_text("ORIGINAL_SENTINEL", encoding="utf-8")
    before = digest(sentinel)
    steps = [
        {"id": "step_00", "language": "python", "purpose": "plant next log link",
         "outputs": ["tables/first.csv"]},
        {"id": "step_01", "language": "python", "purpose": "must not run",
         "outputs": ["tables/second.csv"]},
    ]
    store, state = approved_plan(project_root, steps)
    script0 = (
        "import os\nfrom pathlib import Path\n"
        "task=Path(os.environ['TASK_DIR'])\n"
        f"(task/'logs'/'step_01.log').symlink_to(Path({str(sentinel)!r}))\n"
        "(task/'tables'/'first.csv').write_text('value\\n42\\n')\n")
    script1 = (
        "import os\nfrom pathlib import Path\n"
        "(Path(os.environ['TASK_DIR'])/'tables/second.csv').write_text('value\\n1\\n')\n")
    install_codegen(monkeypatch, {"step_00": script0, "step_01": script1})
    with pytest.raises(OSError):
        task_runner.run(project_root, state["task_id"], client=object())
    assert digest(sentinel) == before
    task = project_root / "analysis" / state["task_id"]
    assert not (task / "tables/second.csv").exists()
    assert store.get(state["task_id"])["status"] == "failed"


def test_task_report_refuses_reports_directory_link(project_root, monkeypatch):
    external = project_root / "external"
    external.mkdir()
    sentinel = external / "sentinel.txt"
    sentinel.write_text("ORIGINAL_SENTINEL", encoding="utf-8")
    before = digest(sentinel)
    steps = [{"id": "step_00", "language": "python", "purpose": "swap reports",
              "outputs": ["tables/first.csv"]}]
    store, state = approved_plan(project_root, steps)
    script = (
        "import os\nfrom pathlib import Path\n"
        "task=Path(os.environ['TASK_DIR'])\n"
        "(task/'tables'/'first.csv').write_text('value\\n42\\n')\n"
        "os.rmdir(task/'reports')\n"
        f"(task/'reports').symlink_to(Path({str(external)!r}))\n")
    install_codegen(monkeypatch, {"step_00": script})
    with pytest.raises((OSError, ValueError)):
        task_runner.run(project_root, state["task_id"], client=object())
    assert digest(sentinel) == before
    assert not (external / "task_report.md").exists()
    assert store.get(state["task_id"])["status"] == "failed"

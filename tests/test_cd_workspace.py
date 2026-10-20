"""Workspace switching and /cd behaviour without model or R execution."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from multiomics_tui.controller import Session
from multiomics_tui.workspace import WorkspaceError, initialize, is_workspace, resolve_target


def make_session(root, cfg, probe=None):
    return Session(root, cfg=cfg, allow_llm=False, client=None, app_root=root,
                   probe=probe if probe is not None else (lambda target: None))


def test_cd_without_argument_reports_current_root(project_root, stub_cfg):
    session = make_session(project_root, stub_cfg)
    response = session.switch_root("")
    assert response.kind == "text"
    assert str(project_root) in response.message
    assert session.root == project_root


def test_cd_initializes_a_new_workspace(project_root, stub_cfg, tmp_path):
    session = make_session(project_root, stub_cfg)
    target = tmp_path / "new workspace"
    target.mkdir()
    response = session.switch_root(str(target))
    assert response.kind == "cwd"
    assert session.root == target.resolve()
    assert (target / "config/study_config.yaml").is_file()
    assert (target / "config/api.local.yaml").is_file()
    assert (target / "data/raw").is_dir()
    assert session.active_task is None
    assert session.tasks.root == target.resolve()
    assert "Initialized:" in response.message


def test_cd_does_not_overwrite_existing_files(project_root, stub_cfg, tmp_path):
    session = make_session(project_root, stub_cfg)
    target = tmp_path / "keep"
    (target / "config").mkdir(parents=True)
    custom = "custom: true\n"
    (target / "config/study_config.yaml").write_text(custom, encoding="utf-8")
    (target / "keep.txt").write_text("keep\n", encoding="utf-8")
    response = session.switch_root(str(target))
    assert response.kind == "cwd"
    assert (target / "config/study_config.yaml").read_text(encoding="utf-8") == custom
    assert (target / "keep.txt").read_text(encoding="utf-8") == "keep\n"
    assert is_workspace(target)


def test_cd_relative_and_quoted_path(project_root, stub_cfg):
    session = make_session(project_root, stub_cfg)
    target = project_root / "study two"
    target.mkdir()
    response = session.switch_root('"study two"')
    assert response.kind == "cwd"
    assert session.root == target.resolve()


def test_cd_expands_home(project_root, stub_cfg, tmp_path, monkeypatch):
    home = tmp_path / "home"
    target = home / "study"
    target.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    session = make_session(project_root, stub_cfg)
    response = session.switch_root("~/study")
    assert response.kind == "cwd"
    assert session.root == target.resolve()


def test_cd_rejects_missing_path(project_root, stub_cfg):
    session = make_session(project_root, stub_cfg)
    response = session.switch_root("does-not-exist")
    assert response.kind == "text"
    assert "Cannot change directory" in response.message
    assert session.root == project_root


def test_cd_rejects_active_job_in_current_workspace(project_root, stub_cfg, tmp_path, monkeypatch):
    session = make_session(project_root, stub_cfg)
    target = tmp_path / "target"
    target.mkdir()
    monkeypatch.setattr(session.jobs, "list_jobs",
                        lambda limit=50: [{"job_id": "job_active", "status": "running"}])
    response = session.switch_root(str(target))
    assert response.kind == "text"
    assert "job_active" in response.message
    assert session.root == project_root


def test_cd_rejects_active_job_in_target_workspace(project_root, stub_cfg, tmp_path):
    session = make_session(project_root, stub_cfg)
    target = tmp_path / "busy"
    jobs = target / "workspace/tui/jobs"
    jobs.mkdir(parents=True)
    (jobs / "job_20260101_000000_deadbeef.json").write_text(json.dumps({
        "version": 2, "job_id": "job_20260101_000000_deadbeef", "kind": "task",
        "status": "running", "supervisor_pid": 999999, "supervisor_identity": {},
    }), encoding="utf-8")
    response = session.switch_root(str(target))
    assert response.kind == "text"
    assert "active job" in response.message
    assert session.root == project_root


def test_cd_probe_failure_keeps_old_root(project_root, stub_cfg, tmp_path):
    def fail(target):
        raise RuntimeError("probe denied")
    session = make_session(project_root, stub_cfg, probe=fail)
    target = tmp_path / "unchecked"
    target.mkdir()
    response = session.switch_root(str(target))
    assert response.kind == "text"
    assert "Workspace check failed" in response.message
    assert session.root == project_root


def test_cd_previous_directory(project_root, stub_cfg, tmp_path):
    session = make_session(project_root, stub_cfg)
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    assert session.switch_root(str(first)).kind == "cwd"
    assert session.switch_root(str(second)).kind == "cwd"
    assert session.switch_root("-").kind == "cwd"
    assert session.root == first.resolve()


def test_cd_is_not_a_read_only_command(project_root, stub_cfg, tmp_path):
    session = make_session(project_root, stub_cfg)
    response = session.readonly_command(f"/cd {tmp_path}")
    assert response.kind == "text"
    assert "not allowed" in response.message
    assert session.root == project_root


def test_cd_is_not_written_to_the_old_task_transcript(project_root, stub_cfg, tmp_path):
    session = make_session(project_root, stub_cfg)
    state = session.tasks.create("old goal")
    session.active_task = state["task_id"]
    old_tasks = session.tasks
    target = tmp_path / "elsewhere"
    target.mkdir()
    response = session.handle(f"/cd {target}")
    assert response.kind == "cwd"
    events = (old_tasks.directory(state["task_id"]) / "events.jsonl").read_text(encoding="utf-8")
    assert "/cd" not in events


def test_workspace_rejects_symlinked_config(project_root, stub_cfg, tmp_path):
    target = tmp_path / "linked"
    target.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (target / "config").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")
    with pytest.raises(ValueError):
        initialize(target, project_root)


def test_resolve_target_rejects_files(project_root, tmp_path):
    path = tmp_path / "file.txt"
    path.write_text("not a directory\n", encoding="utf-8")
    with pytest.raises(WorkspaceError):
        resolve_target(project_root, str(path))


def test_code_root_falls_back_to_installed_package(tmp_path, monkeypatch):
    from native_dist.layout import code_root, resource
    monkeypatch.delenv("MULTIOMICS_APP_ROOT", raising=False)
    assert code_root(tmp_path) == Path(__file__).resolve().parents[1]
    assert resource(tmp_path, "multiomics_tui/task_guard.py").is_file()


def test_code_root_prefers_a_full_workspace(tmp_path, monkeypatch):
    from native_dist.layout import code_root
    monkeypatch.delenv("MULTIOMICS_APP_ROOT", raising=False)
    (tmp_path / "multiomics_tui").mkdir()
    (tmp_path / "multiomics_tui/task_guard.py").write_text("# fake\n", encoding="utf-8")
    assert code_root(tmp_path) == tmp_path.resolve()


def test_help_and_completion_list_include_cd():
    from multiomics_tui.controller import HELP_TEXT
    from multiomics_tui.app import COMMAND_LIST
    assert "/cd" in HELP_TEXT
    assert "/cd" in COMMAND_LIST


def test_step_environment_reports_code_runtime_root(project_root, stub_cfg):
    from multiomics_tui.task_runner import _step_environment, file_hash
    from native_dist.layout import code_root
    source = project_root / "data/raw/runtime.csv"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("sample,value\na,1\n", encoding="utf-8")
    task = project_root / "analysis/task_runtime"
    task.mkdir(parents=True)
    env = _step_environment(project_root, task, stub_cfg,
                            [{"path": "data/raw/runtime.csv", "sha256": file_hash(source)}])
    assert Path(env["MULTIOMICS_RUNTIME_ROOT"]).resolve() == code_root(project_root).resolve()


def test_windows_launch_paths_uses_the_installation_runtime(tmp_path):
    from multiomics_tui.windows_native import launch_paths
    workspace = tmp_path / "workspace"
    task = workspace / "analysis/task_fixture"
    task.mkdir(parents=True)
    view = workspace / ".runtime/inputs/task_fixture"
    view.mkdir(parents=True)
    install = tmp_path / "install"
    runtime = install / ".runtime/sandbox_runtime"
    runtime.mkdir(parents=True)
    env = {"MULTIOMICS_WORKSPACE_ROOT": str(workspace),
           "MULTIOMICS_RUNTIME_ROOT": str(install),
           "PROJECT_ROOT": str(view)}
    root, found, found_view = launch_paths(task, env)
    assert root == workspace.resolve()
    assert found == runtime.resolve()
    assert found_view == view.resolve()


def test_cd_resets_the_tui_to_home(project_root, stub_cfg, tmp_path):
    pytest.importorskip("textual")
    from multiomics_tui.app import MultiomicsApp

    async def scenario():
        session = make_session(project_root, stub_cfg)
        app = MultiomicsApp(session)
        target = tmp_path / "switch"
        target.mkdir()
        async with app.run_test(size=(80, 24)) as pilot:
            await app._handle_async(f"/cd {target}", submitted=True)
            await pilot.pause()
            assert session.root == target.resolve()
            assert app._home_visible is True
            assert session.active_task is None
            assert "switch" in app.sub_title
            assert "switch" in str(app.query_one("#status-bar").renderable)

    asyncio.run(scenario())

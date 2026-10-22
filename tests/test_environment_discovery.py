"""First-install discovery must prefer user-owned interpreters and libraries."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from native_dist import bootstrap, environment


def _touch(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_python_candidates_include_user_managed_interpreter(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData/Local"))
    if os.name == "nt":
        user_python = _touch(tmp_path / "AppData/Local/uv/python/cpython-3.14.0/python.exe")
    else:
        user_python = _touch(tmp_path / ".local/share/uv/python/cpython-3.14.0/bin/python3")
    found = environment.python_candidates(tmp_path / "install")
    assert str(user_python.absolute()) in found
    assert found.index(str(user_python.absolute())) < 5


def test_r_candidates_include_user_managed_rscript(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData/Local"))
    if os.name == "nt":
        user_r = _touch(tmp_path / "scoop/apps/r/current/bin/Rscript.exe")
    else:
        user_r = _touch(tmp_path / ".local/bin/Rscript")
    found = environment.r_candidates(tmp_path / "install")
    assert str(user_r.absolute()) in found


def test_inspect_python_reuses_user_site(tmp_path, monkeypatch):
    userbase = tmp_path / "userbase"
    probe = subprocess.check_output(
        [sys.executable, "-c", "import site;print(site.getusersitepackages())"],
        text=True, env={**os.environ, "PYTHONUSERBASE": str(userbase)}).strip()
    user_site = Path(probe)
    (user_site / "demo_package").mkdir(parents=True)
    (user_site / "demo_package/__init__.py").write_text("", encoding="utf-8")
    (user_site / "demo_package-1.0.dist-info").mkdir()
    (user_site / "demo_package-1.0.dist-info/METADATA").write_text(
        "Metadata-Version: 2.1\nName: demo-package\nVersion: 1.0\n", encoding="utf-8")
    monkeypatch.setenv("PYTHONUSERBASE", str(userbase))
    monkeypatch.delenv("PYTHONNOUSERSITE", raising=False)
    info = environment.inspect_python(sys.executable, ["demo-package"])
    assert info["packages"][0]["status"] == "reusable"
    assert str(user_site.resolve()) in {str(Path(p).resolve()) for p in info["library_paths"]}


def test_inspect_r_includes_user_library(tmp_path, monkeypatch):
    rscript = shutil.which("Rscript")
    if not rscript:
        pytest.skip("Rscript is unavailable")
    user_lib = tmp_path / "R-library"
    user_lib.mkdir()
    monkeypatch.setenv("R_LIBS_USER", str(user_lib))
    root = tmp_path / "project"
    root.mkdir()
    info = environment.inspect_r(Path(rscript), root)
    if not info.get("compatible"):
        pytest.fail("R inspection failed: " + (info.get("stderr") or repr(info)))
    libraries = {str(Path(p).resolve()) for p in info.get("libraries", [])}
    assert str(user_lib.resolve()) in libraries


def test_choose_candidate_prefers_complete_package_set():
    rows = [
        {"path": "system", "compatible": True,
         "packages": [{"status": "reusable"}, {"status": "reusable"}]},
        {"path": "user", "compatible": True,
         "packages": [{"status": "missing"}]},
        {"path": "partial", "compatible": True,
         "packages": [{"status": "reusable"}, {"status": "missing"}]},
    ]
    assert environment.choose_candidate(rows)["path"] == "system"


def test_choose_candidate_falls_back_to_most_reusable():
    rows = [
        {"path": "system", "compatible": True,
         "packages": [{"status": "missing"}, {"status": "missing"}]},
        {"path": "user", "compatible": True,
         "packages": [{"status": "reusable"}, {"status": "missing"}]},
    ]
    assert environment.choose_candidate(rows)["path"] == "user"
    assert environment.choose_candidate([]) is None


def test_explicit_interpreter_override_wins(tmp_path, monkeypatch):
    explicit = tmp_path / "python"
    explicit.write_text("", encoding="utf-8")
    monkeypatch.setenv("MULTIOMICS_PYTHON", str(explicit))
    rows = [
        {"path": str(tmp_path / "other"), "compatible": True, "packages": []},
        {"path": str(explicit), "compatible": True, "packages": []},
    ]
    assert environment._preferred_candidate(rows, "MULTIOMICS_PYTHON") is rows[1]


@pytest.mark.skipif(os.name == "nt", reason="POSIX helper test")
def test_reused_library_pth_is_written_for_existing_venv(tmp_path, monkeypatch):
    root = tmp_path / "project"
    venv = root / ".venv"
    purelib = venv / "lib/python3.14/site-packages"
    purelib.mkdir(parents=True)
    user_site = tmp_path / "user/lib/python3.14/site-packages"
    user_site.mkdir(parents=True)
    py = venv / "bin/python"
    py.parent.mkdir(parents=True)
    py.write_text("", encoding="utf-8")
    monkeypatch.setattr(bootstrap, "captured_run",
                        lambda argv, **kwargs: SimpleNamespace(stdout=str(purelib) + "\n"))
    reused = bootstrap._write_reused_libraries(py, root, {
        "library_paths": [str(purelib), str(user_site)],
    })
    assert reused == [str(user_site.resolve())]
    target = purelib / "multiomics_reused_libraries.pth"
    assert target.read_text(encoding="utf-8") == str(user_site.resolve()) + "\n"

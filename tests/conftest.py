"""Shared fixtures for the multiomics TUI tests (offline, no network, no API key)."""
from pathlib import Path
import shutil
import json

import pytest


def build_project(tmp_path: Path) -> Path:
    (tmp_path / "config").mkdir()
    (tmp_path / "config/study_config.yaml").write_text(
        "schema_version: '1.0'\n"
        f"runtime:\n  rscript: {json.dumps(shutil.which('Rscript') or 'Rscript')}\n  workers: 2\n"
        "llm:\n  enabled: false\n  api_key_env: TEST_DEEPSEEK_KEY\n  model: stub\n",
        encoding="utf-8")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/00_run.sh").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    for name in ("go_gsva_prognosis", "go_gsva_optimal_cutoff", "go_gsva_optimal_cutoff/ne_km",
                 "go_gsva_gobp2026"):
        folder = tmp_path / "analysis" / name
        folder.mkdir(parents=True)
        (folder / "run_all.sh").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    (tmp_path / "workspace/runs").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def project_root(tmp_path):
    return build_project(tmp_path)


@pytest.fixture
def stub_cfg():
    return {
        "runtime": {"rscript": shutil.which("Rscript") or "Rscript", "workers": 2},
        "llm": {"enabled": False, "api_key_env": "TEST_DEEPSEEK_KEY", "model": "stub"},
    }

"""Shared fixtures for the multiomics TUI tests (offline, no network, no API key)."""
from pathlib import Path
import shutil
import json

import pytest


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "real_probe: keep the real native_dist.doctor.require_probe (no substitution)")


@pytest.fixture(autouse=True)
def substituted_probe(request, monkeypatch):
    """Unit tests never execute model-generated code, so the machine probe is replaced.

    require_probe needs a passed .runtime/doctor.json bound to the install root; a
    throwaway tmp project root can never satisfy it off Linux, which used to abort
    planning before any assertion ran. Real probe behaviour is covered by
    test_v8_distribution.py::test_probe_record_is_required_and_fingerprint_bound,
    and by native doctor acceptance.
    """
    if request.node.get_closest_marker("real_probe"):
        return
    from native_dist import doctor
    # Both call sites import the function lazily, so patching the module attribute works.
    monkeypatch.setattr(doctor, "require_probe", lambda root: None)


def build_project(tmp_path: Path) -> Path:
    # macOS 上 /var 是 /private/var 的别名。Seatbelt 授权按解析后的真实路径生成，
    # 用未解析的拼写启动 R 会被拒绝（Operation not permitted），因此这里统一解析。
    tmp_path = Path(tmp_path).resolve()
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


def project_r_library_paths() -> list[str]:
    """开发机上复用 bootstrap 生成的 R 库；没有该文件时返回空列表。

    输入结构探测需要 R 的 jsonlite。用 bootstrap 安装的机器上它位于
    .runtime/r-library，不在系统库；CI/发行 ZIP 用系统库（CI 已装 jsonlite）。
    """
    path = Path(__file__).resolve().parents[1] / "config/runtime.local.yaml"
    try:
        import yaml
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, ImportError, ValueError):
        return []
    runtime = value.get("runtime") if isinstance(value, dict) else None
    if not isinstance(runtime, dict):
        return []
    return [str(p) for p in runtime.get("r_library_paths") or [] if Path(p).is_dir()]


@pytest.fixture
def project_root(tmp_path):
    return build_project(tmp_path)


@pytest.fixture
def stub_cfg():
    return {
        "runtime": {"rscript": shutil.which("Rscript") or "Rscript", "workers": 2,
                    "r_library_paths": project_r_library_paths()},
        "llm": {"enabled": False, "api_key_env": "TEST_DEEPSEEK_KEY", "model": "stub"},
    }

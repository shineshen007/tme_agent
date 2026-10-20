"""Shared fixtures for the multiomics TUI tests (offline, no network, no API key)."""
from functools import lru_cache
import json
import os
from pathlib import Path
import shutil
import subprocess

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


@pytest.fixture(autouse=True)
def substituted_windows_guard(monkeypatch):
    """Windows 单元测试不经过 AppContainer。

    AppContainer 需要 bootstrap 准备的私有运行时与输入快照，而且运行在独立子进程
    里、无法在测试进程内替换。这里替换父进程侧的接缝，直接运行探测命令；原生
    隔离仍由 bootstrap 与 Windows 原生验收覆盖。
    """
    if os.name != "nt":
        return
    from multiomics_tui import input_inspection

    def run_without_appcontainer(root, folder, argv, env):
        # argv = [python, task_guard.py, folder, '--', rscript, ...]
        return input_inspection.subprocess.run(
            argv[argv.index("--") + 1:], cwd=root, env=env,
            capture_output=True, text=True, timeout=120)

    monkeypatch.setattr(input_inspection, "_run_guarded", run_without_appcontainer)


@lru_cache(maxsize=1)
def host_r_environment():
    """宿主 R 的 rscript、R_HOME 与真实库路径，整个测试会话只探测一次。

    _step_environment 把 HOME 指向任务目录，R 因此读不到 ~/.Renviron 里配置的
    用户库（CI 用 setup-r 安装的 jsonlite 就在那里）。测试必须显式传入库路径，
    才能和真实安装一致；未安装 Rscript 时返回空列表，由用例自行 skip。
    """
    rscript = shutil.which("Rscript") or "Rscript"

    def probe(expression: str) -> list[str]:
        try:
            result = subprocess.run([rscript, "--vanilla", "-e", expression],
                                    capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    home = (probe("cat(R.home())") or [""])[0]
    libraries = [p for p in probe('cat(.libPaths(), sep="\\n")') if p and Path(p).is_dir()]
    return rscript, home, libraries


def project_r_library_paths() -> list[str]:
    """bootstrap 生成的项目 R 库；没有 config/runtime.local.yaml 时返回空列表。

    开发机上 jsonlite 装在 .runtime/r-library，不在系统库；CI 则由 setup-r 装入
    用户库。两者都要并入临时项目配置，输入探测才能在任意机器上找到依赖。
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


def runtime_settings() -> dict:
    """临时项目与 stub_cfg 共用：宿主 R 解释器、R_HOME 和全部可见库。"""
    rscript, home, host_libraries = host_r_environment()
    libraries = list(dict.fromkeys([*project_r_library_paths(), *host_libraries]))
    runtime = {"rscript": rscript, "workers": 2, "r_library_paths": libraries}
    if home:
        runtime["r_home"] = home
    return runtime


def build_project(tmp_path: Path) -> Path:
    # macOS 上 /var 是 /private/var 的别名。Seatbelt 授权按解析后的真实路径生成，
    # 用未解析的拼写启动 R 会被拒绝（Operation not permitted），因此这里统一解析。
    tmp_path = Path(tmp_path).resolve()
    runtime = runtime_settings()
    lines = ["schema_version: '1.0'", "runtime:",
             "  rscript: " + json.dumps(runtime["rscript"]),
             "  workers: 2"]
    if runtime.get("r_home"):
        lines.insert(3, "  r_home: " + json.dumps(runtime["r_home"]))
    if runtime["r_library_paths"]:
        lines.append("  r_library_paths:")
        lines.extend("    - " + json.dumps(p) for p in runtime["r_library_paths"])
    lines.extend(["llm:", "  enabled: false", "  api_key_env: TEST_DEEPSEEK_KEY", "  model: stub"])
    (tmp_path / "config").mkdir()
    (tmp_path / "config/study_config.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
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
        "runtime": runtime_settings(),
        "llm": {"enabled": False, "api_key_env": "TEST_DEEPSEEK_KEY", "model": "stub"},
    }


@pytest.fixture
def isolated_r_env(monkeypatch):
    """R 库环境变量只影响真实安装，不应泄漏进判定"可用包"的用例。"""
    for name in ("R_LIBS", "R_LIBS_USER", "R_LIBS_SITE"):
        monkeypatch.delenv(name, raising=False)

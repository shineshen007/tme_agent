"""Action whitelist: natural language and slash commands may only reach these.

Every command is built as an argv list (never a shell string), run-id and module
arguments are validated, and module names must come from the ``analysis`` tree.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}")


@dataclass(frozen=True)
class ActionSpec:
    id: str
    label: str
    kind: str  # readonly | mutating | costly
    requires_key: bool = False
    confirm: bool = False
    note: str = ""


SPECS: dict[str, ActionSpec] = {
    "run_data": ActionSpec(
        "run_data", "Input validation and sample registry (--stage data)", "mutating",
        confirm=True, note="Usually minutes; no LLM or API key required."),
    "run_all": ActionSpec(
        "run_all", "Full pipeline (--stage all)", "costly", requires_key=True, confirm=True,
        note="May take hours; requires DEEPSEEK_API_KEY; raw data is read-only."),
    "resume": ActionSpec(
        "resume", "Resume run (resume --stage all)", "costly", requires_key=True, confirm=True,
        note="Input, code, and environment fingerprints must match; no concurrent run with the same ID."),
    "verify": ActionSpec(
        "verify", "Verify run fingerprint, artifact hashes, and result contract", "readonly",
        note="Read-only; does not change artifacts."),
    "dossiers": ActionSpec(
        "dossiers", "Build and publish target dossiers to result/", "mutating", confirm=True,
        note="Publication follows config; cross-run conflicts may require --force."),
    "verify_dossiers": ActionSpec(
        "verify_dossiers", "Verify published target dossiers", "readonly",
        note="Read-only byte-for-byte rerender check; may take minutes."),
    "module_run": ActionSpec(
        "module_run", "Run analysis module (run_all.sh)", "costly", confirm=True,
        note="R computation may take minutes to hours and writes to the module directory."),
}


# Preserve historical job records while rendering their former UI labels in English.
LEGACY_JOB_LABELS = {
    "输入校验与样本注册（--stage data）": "run_data",
    "完整流程（--stage all）": "run_all",
    "恢复已有运行（resume --stage all）": "resume",
    "校验运行指纹、产物哈希与差异契约": "verify",
    "构建并发布靶点证据包到 result/": "dossiers",
    "校验已发布靶点证据包": "verify_dossiers",
    "运行 analysis 独立模块（run_all.sh）": "module_run",
}


def display_job_label(label: str | None) -> str:
    value = str(label or "")
    action_id = LEGACY_JOB_LABELS.get(value)
    return SPECS[action_id].label if action_id else value


class ActionError(ValueError):
    pass


def new_run_id(prefix: str = "tui") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{stamp}"


def validate_run_id(value) -> str:
    value = str(value or "").strip()
    if not value or not RUN_ID_RE.fullmatch(value):
        raise ActionError("Invalid run-id: use letters, digits, underscore, period, or hyphen (at most 101 characters)")
    return value


def list_modules(root) -> list[str]:
    """Enumerate whitelisted module names relative to analysis/ (one or two levels)."""
    base = Path(root) / "analysis"
    if not base.is_dir():
        return []
    names = []
    for pattern in ("*/run_all.sh", "*/*/run_all.sh"):
        for script in base.glob(pattern):
            names.append(script.parent.relative_to(base).as_posix())
    return sorted(set(names))


def validate_module(root, name) -> str:
    name = str(name or "").strip().strip("/")
    modules = list_modules(root)
    if name not in modules:
        raise ActionError(f"Unknown module {name!r}; available modules: {modules or '(none)'}")
    base = (Path(root) / "analysis").resolve()
    script = (base / name / "run_all.sh").resolve()
    if not script.is_file() or not script.is_relative_to(base):
        raise ActionError(f"Module script is missing or outside analysis/: {name}")
    return name


def rscript_path(root) -> str:
    root = Path(root)
    value = None
    local = root / "config/runtime.local.yaml"
    if local.is_file():
        try:
            value = ((yaml.safe_load(local.read_text(encoding="utf-8")) or {})
                     .get("runtime", {}) or {}).get("rscript")
        except yaml.YAMLError:
            value = None
    if not value:
        try:
            value = ((yaml.safe_load((root / "config/study_config.yaml").read_text(encoding="utf-8")) or {})
                     .get("runtime", {}) or {}).get("rscript")
        except (OSError, yaml.YAMLError):
            value = None
    if value and Path(value).is_absolute():
        return str(value)
    default = Path("/usr/lib/R/bin/Rscript")
    if default.is_file():
        return str(default)
    return str(value or "Rscript")


def build_command(root, action_id: str, run_id=None, module=None, force: bool = False) -> list[str]:
    """Build the argv for one whitelisted action. Raises ActionError otherwise."""
    root = Path(root)
    from .platform_support import require_supported_action
    require_supported_action(root, action_id)
    if action_id not in SPECS:
        raise ActionError(f"Unknown action {action_id!r}")
    if action_id == "module_run":
        name = validate_module(root, module)
        return ["bash", str(root / "analysis" / name / "run_all.sh"), rscript_path(root)]
    rid = validate_run_id(run_id or new_run_id())
    runner = root / "scripts/00_run.sh"
    if not runner.is_file():
        raise ActionError(f"Runner is missing: {runner}")
    if action_id == "run_data":
        return ["bash", str(runner), "run", "--run-id", rid, "--stage", "data"]
    if action_id == "run_all":
        return ["bash", str(runner), "run", "--run-id", rid, "--stage", "all"]
    if action_id == "resume":
        return ["bash", str(runner), "resume", "--run-id", rid, "--stage", "all"]
    if action_id == "verify":
        return ["bash", str(runner), "verify", "--run-id", rid]
    if action_id == "dossiers":
        command = ["bash", str(runner), "dossiers", "--run-id", rid]
        if force:
            command.append("--force")
        return command
    if action_id == "verify_dossiers":
        return ["bash", str(runner), "verify-dossiers", "--run-id", rid]
    raise ActionError(f"Action {action_id!r} is not implemented")

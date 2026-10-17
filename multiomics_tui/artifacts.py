"""Read-only enumeration and rendering of frozen artifacts.

Nothing here writes files or recomputes statistics: summaries show verbatim
values copied from run state, manifests and reports.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

FIGURE_SUFFIXES = {".pdf", ".svg"}


def _read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def runs_dir(root) -> Path:
    return Path(root) / "workspace/runs"


def list_runs(root) -> list[dict]:
    base = runs_dir(root)
    if not base.is_dir():
        return []
    runs = []
    for run in sorted(base.iterdir()):
        if not run.is_dir() or run.name.startswith("."):
            continue
        state = _read_json(run / "run_state.json", {}) or {}
        saved = _read_json(run / "run.json", {}) or {}
        runs.append({
            "run_id": run.name,
            "status": state.get("status") or state.get("technical_status") or "unknown",
            "technical_status": state.get("technical_status"),
            "scientific_status": state.get("scientific_status"),
            "updated_at": state.get("updated_at"),
            "error": state.get("error"),
            "fingerprint": (saved.get("fingerprint") or "")[:12],
            "has_manifest": (run / "artifact_manifest.json").is_file(),
            "path": str(run),
            "mtime": run.stat().st_mtime,
        })
    runs.sort(key=lambda item: item["mtime"], reverse=True)
    return runs


def get_run(root, run_id) -> dict | None:
    for run in list_runs(root):
        if run["run_id"] == run_id:
            return run
    return None


def latest_run(root, completed_only: bool = False) -> dict | None:
    for run in list_runs(root):
        if run["status"] in {"running", "failed"}:
            if completed_only:
                continue
        return run
    return None


def newest_run_id(root, exclude_prefixes=("tui_",)) -> str | None:
    """Most recent run that is not an obviously scratch run; fallback newest overall."""
    runs = list_runs(root)
    for run in runs:
        if not run["run_id"].startswith(tuple(exclude_prefixes)):
            return run["run_id"]
    return runs[0]["run_id"] if runs else None


def report_paths(root, run_id) -> list[Path]:
    run = runs_dir(root) / run_id
    candidates = [
        run / "report/summary.md",
        run / "report/agent_report.md",
        run / "report/evidence_status.json",
        run / "run_state.json",
    ]
    return [path for path in candidates if path.is_file()]


def result_targets(root) -> list[str]:
    base = Path(root) / "result"
    if not base.is_dir():
        return []
    return sorted(item.name for item in base.iterdir()
                  if item.is_dir() and not item.name.startswith("."))


def analysis_reports(root) -> list[Path]:
    base = Path(root) / "analysis"
    if not base.is_dir():
        return []
    found = []
    for pattern in ("*/reports/*.md", "*/*/reports/*.md"):
        found.extend(sorted(base.glob(pattern)))
    return found


def list_figures(root, limit: int = 200) -> list[dict]:
    root = Path(root)
    found = []
    for pattern in ("workspace/runs/*/figures/**/*",):
        for path in sorted(root.glob(pattern)):
            if path.is_file() and path.suffix.lower() in FIGURE_SUFFIXES:
                found.append({"path": path.relative_to(root).as_posix(), "source": "run"})
    base = root / "analysis"
    if base.is_dir():
        for path in sorted(base.glob("**/figures/**/*")):
            if path.is_file() and path.suffix.lower() in FIGURE_SUFFIXES:
                found.append({"path": path.relative_to(root).as_posix(), "source": "module"})
    for path in sorted((root / "result").glob("*/figures/**/*")):
        if path.is_file() and path.suffix.lower() in FIGURE_SUFFIXES:
            found.append({"path": path.relative_to(root).as_posix(), "source": "dossier"})
    return found[:limit]


def read_text(path: Path, limit: int = 200_000) -> str:
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[:limit]


def registry_ids(root, run_id=None) -> list[str]:
    """Cohort identifiers used to block private ids from outbound LLM payloads."""
    run_id = run_id or (latest_run(root) or {}).get("run_id")
    if not run_id:
        return []
    path = runs_dir(root) / run_id / "inputs/sample_registry.csv"
    if not path.is_file():
        return []
    ids = set()
    try:
        with path.open(newline="", encoding="utf-8", errors="replace") as handle:
            for row in csv.DictReader(handle):
                for key in ("sample_id", "patient_id", "biospecimen_id", "sample", "patient"):
                    value = (row.get(key) or "").strip()
                    if value:
                        ids.add(value)
    except OSError:
        return []
    return sorted(ids)


def run_summary(root, run_id=None) -> str:
    run = get_run(root, run_id) if run_id else latest_run(root)
    if run is None:
        return "No runs found in workspace/runs/."
    lines = [
        f"Run: {run['run_id']}",
        f"Status: {run['status']}",
        f"Technical status: {run['technical_status'] or '(not recorded)'}",
        f"Scientific status: {run['scientific_status'] or '(not recorded)'}",
        f"Updated: {run['updated_at'] or '(unknown)'}",
        f"Fingerprint: {run['fingerprint'] or '(none)'}",
        f"Artifact manifest: {'yes' if run['has_manifest'] else 'no'}",
        f"Path: {run['path']}",
    ]
    if run.get("error"):
        lines.append(f"Error: {run['error']}")
    return "\n".join(lines)


def runs_table(root, limit: int = 30) -> str:
    runs = list_runs(root)[:limit]
    if not runs:
        return "No runs found."
    lines = [f"{'run_id':<46} {'status':<28} updated_at"]
    for run in runs:
        lines.append(f"{run['run_id']:<46} {str(run['status']):<28} {run['updated_at'] or ''}")
    return "\n".join(lines)


def figures_table(root, limit: int = 200) -> str:
    figures = list_figures(root, limit=limit)
    if not figures:
        return "No PDF/SVG figures found."
    return "\n".join(f"[{item['source']}] {item['path']}" for item in figures)

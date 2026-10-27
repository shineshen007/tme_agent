"""Built-in datasets shipped with the release and seeded into workspaces.

A built-in dataset is data, not application code. ``datasets/<id>/`` holds the
frozen files plus a ``dataset.json`` manifest; every workspace receives a copy
under ``data/raw/<id>/`` and ``data/metadata/<id>/`` so natural-language tasks can
bind them like any user input.

The module only reads and copies: it never fetches, transforms, or overwrites.
Workspace seeding reuses the exclusive-copy helper so an existing user file is
kept byte-for-byte, and a manifest mismatch is reported instead of silently
replacing data.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

MANIFEST = "dataset.json"
# Seed targets are the only paths a dataset may write into a workspace. Keeping
# them explicit means a manifest cannot smuggle files into analysis/ or config/.
SEED_PREFIXES = ("data/raw/", "data/metadata/")
REQUIRED_KEYS = ("schema_version", "dataset_id", "title", "provenance_status", "source",
                 "license", "files", "samples", "groups", "bindings", "quality_flags", "build")


class DatasetError(ValueError):
    """A built-in dataset is missing, malformed, or does not match its manifest."""


def digest(path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def datasets_root(workspace) -> Path:
    """The installed ``datasets/`` directory, independent of the current workspace."""
    from native_dist.layout import code_root
    return code_root(Path(workspace)) / "datasets"


def _manifest(directory: Path) -> dict:
    path = directory / MANIFEST
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise DatasetError(f"Cannot read {path}: {exc}") from exc
    except ValueError as exc:
        raise DatasetError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise DatasetError(f"{path} must contain a JSON object")
    missing = [key for key in REQUIRED_KEYS if key not in value]
    if missing:
        raise DatasetError(f"{path} is missing fields: {', '.join(missing)}")
    if value["schema_version"] != 1:
        raise DatasetError(f"Unsupported dataset schema_version in {path}")
    if not isinstance(value["files"], list) or not value["files"]:
        raise DatasetError(f"{path} declares no data files")
    return value


def entry(directory: Path) -> dict:
    """Validate one dataset directory against its manifest and return the record."""
    directory = Path(directory)
    manifest = _manifest(directory)
    for item in manifest["files"]:
        relative = str(item.get("path", ""))
        if not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise DatasetError(f"Unsafe dataset file path: {relative!r}")
        path = directory / relative
        if path.is_symlink() or not path.is_file():
            raise DatasetError(f"Declared dataset file is missing: {relative}")
        if path.stat().st_size != item.get("bytes"):
            raise DatasetError(f"Dataset file size differs from manifest: {relative}")
        if digest(path) != item.get("sha256"):
            raise DatasetError(f"Dataset file checksum differs from manifest: {relative}")
    return {"directory": directory, "manifest": manifest}


def catalog(workspace) -> list[dict]:
    """Every installed dataset that matches its manifest, sorted by dataset_id.

    A release without a ``datasets/`` directory is normal (source checkout before
    the first build), so a missing directory yields an empty list rather than an
    error. A present but corrupt dataset is an error: silently skipping it would
    hide a broken install.
    """
    root = datasets_root(workspace)
    if not root.is_dir():
        return []
    found = []
    for directory in sorted(root.iterdir()):
        if not directory.is_dir() or not (directory / MANIFEST).is_file():
            continue
        found.append(entry(directory))
    return sorted(found, key=lambda item: item["manifest"]["dataset_id"])


def seeds(workspace) -> dict[str, str]:
    """Target workspace path -> source path inside ``datasets/``.

    Matrix files land in ``data/raw/<id>/`` and tables in ``data/metadata/<id>/``;
    the manifest's ``bindings`` already point at those exact relative paths, so a
    seeded workspace needs no rewrite before planning.
    """
    mapping: dict[str, str] = {}
    for item in catalog(workspace):
        directory = item["directory"]
        dataset_id = item["manifest"]["dataset_id"]
        for record in item["manifest"]["files"]:
            relative = str(record["path"])
            folder = "data/metadata" if relative.startswith("metadata/") else "data/raw"
            name = relative.split("/", 1)[1] if "/" in relative else relative
            target = f"{folder}/{dataset_id}/{name}"
            if not target.startswith(SEED_PREFIXES):
                raise DatasetError(f"Dataset {dataset_id} would seed outside data/: {target}")
            mapping[target] = str(directory / relative)
    return mapping


def provenance_pending(workspace) -> list[str]:
    """Dataset ids whose upstream provenance is still ``not_recorded``."""
    return [item["manifest"]["dataset_id"] for item in catalog(workspace)
            if item["manifest"].get("provenance_status") != "recorded"]


def summary_lines(workspace, language: str = "en") -> list[str]:
    """Human-readable listing for ``/datasets``; never prints sample identifiers."""
    from .language import choose
    items = catalog(workspace)
    if not items:
        return [choose(language,
                       "No built-in datasets are installed under datasets/.",
                       "未安装内置数据集（datasets/ 为空）。")]
    lines: list[str] = []
    for item in items:
        manifest = item["manifest"]
        samples = manifest.get("samples") or {}
        counts = ", ".join(f"{key}={samples[key]}" for key in
                           ("total", "tumor", "normal", "recurrent", "paired_patients")
                           if key in samples)
        lines.append(f"{manifest['dataset_id']}: {manifest['title']}")
        if counts:
            lines.append(choose(language, "  Samples: ", "  样本：") + counts)
        status = manifest.get("provenance_status")
        if status != "recorded":
            lines.append(choose(language,
                                "  Provenance: " + str(status) +
                                " (record source, access tier and retrieval date before release)",
                                "  来源记录：" + str(status) +
                                "（发布前需补齐来源、访问级别与获取日期）"))
        for record in manifest["files"]:
            lines.append(choose(language, "  File: ", "  文件：") +
                         f"{record['path']} ({record['format']}, {record['rows']}x{record['columns']})")
        for name, binding in (manifest.get("bindings") or {}).items():
            lines.append(choose(language, f"  Binding {name}: ", f"  绑定 {name}：") +
                         json.dumps(binding, ensure_ascii=False, sort_keys=True))
        lines.append(choose(language,
                            "  Seed a workspace: /cd <directory> copies the files into data/.",
                            "  写入工作区：/cd <目录> 会把文件复制到 data/。"))
    return lines

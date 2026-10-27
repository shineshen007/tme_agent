"""Download the open-access TCGA-LIHC STAR counts bundle from GDC.

Only the public GDC REST API is used (stdlib ``urllib``), and every byte is
checked against the ``md5sum``/``file_size`` GDC declares before it is accepted.
The bundle is written under ``data/raw/_gdc_cache/`` which is git-ignored; the
build script reads it and nothing here writes into ``datasets/``.

Usage:
  python3 scripts/08_fetch_gdc_tcga_lihc.py --project-root .
  python3 scripts/08_fetch_gdc_tcga_lihc.py --verify-only
  python3 scripts/08_fetch_gdc_tcga_lihc.py --resume

The record in ``fetch_report.json`` pins the GDC data release and commit so a
later rebuild can tell an upstream change apart from a local code change.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.gdc.cancer.gov"
PROJECT = "TCGA-LIHC"
DATA_TYPE = "Gene Expression Quantification"
ACCESS = "open"
CACHE = Path("data/raw/_gdc_cache")

REPORT = "fetch_report.json"
# GDC accepts a list of ids per /data call; keep batches small enough that a
# failure only costs one batch and the retry stays cheap.
BATCH = 150
RETRIES = 3
TIMEOUT = 600


def post_json(url: str, payload: dict) -> dict:
    body = json.dumps(payload).encode()
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.load(response)


def release_status() -> dict:
    with urllib.request.urlopen(f"{API}/status", timeout=60) as response:
        value = json.load(response)
    return {"data_release": value.get("data_release"),
            "gdc_api_version": value.get("tag"),
            "gdc_commit": value.get("commit")}


def files() -> list[dict]:
    """Every open Gene Expression Quantification file with its sample identity."""
    payload = {
        "filters": {"op": "and", "content": [
            {"op": "in", "content": {"field": "cases.project.project_id", "value": [PROJECT]}},
            {"op": "in", "content": {"field": "data_type", "value": [DATA_TYPE]}},
            {"op": "in", "content": {"field": "access", "value": [ACCESS]}}]},
        "fields": ("file_id,file_name,file_size,md5sum,analysis.workflow_type,"
                   "cases.submitter_id,cases.samples.submitter_id,"
                   "cases.samples.sample_type,cases.samples.tissue_type"),
        "size": 2000}
    hits = post_json(f"{API}/files", payload)["data"]["hits"]
    records = []
    for hit in hits:
        case = hit["cases"][0]
        sample = case["samples"][0]
        records.append({"file_id": hit["id"], "file_name": hit["file_name"],
                        "md5": hit["md5sum"], "bytes": hit["file_size"],
                        "workflow": (hit.get("analysis") or {}).get("workflow_type"),
                        "case": case["submitter_id"], "sample": sample["submitter_id"],
                        "sample_type": sample["sample_type"],
                        "tissue_type": sample["tissue_type"]})
    if not records:
        raise SystemExit("GDC returned no files; check the filters")
    if len({r["file_id"] for r in records}) != len(records):
        raise SystemExit("GDC returned duplicate file ids")
    return sorted(records, key=lambda r: r["file_id"])


def md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def fetch_parts(cache: Path, records: list[dict]) -> list[Path]:
    """Download one tar.gz per batch; each part stays a valid archive of its own.

    Concatenating GDC tars would not work: every tar ends with an end-of-archive
    marker, so a reader stops after the first member set. Parts are therefore
    extracted separately into one shared folder instead.
    """
    ids = [r["file_id"] for r in records]
    chunks = [ids[i:i + BATCH] for i in range(0, len(ids), BATCH)]
    parts: list[Path] = []
    for index, chunk in enumerate(chunks, 1):
        part = cache / f"part-{index:03d}.tar.gz"
        if part.is_file() and part.stat().st_size:
            parts.append(part)
            continue
        for attempt in range(1, RETRIES + 1):
            try:
                request = urllib.request.Request(
                    f"{API}/data", data=json.dumps({"ids": chunk}).encode(),
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(request, timeout=TIMEOUT) as response, part.open("wb") as out:
                    while True:
                        block = response.read(1024 * 1024)
                        if not block:
                            break
                        out.write(block)
                print(f"batch {index}/{len(chunks)}: {part.stat().st_size} bytes")
                break
            except (urllib.error.URLError, OSError) as exc:
                part.unlink(missing_ok=True)
                if attempt == RETRIES:
                    raise SystemExit(f"batch {index} failed after {RETRIES} attempts: {exc}") from exc
                print(f"batch {index} attempt {attempt} failed: {exc}; retrying", file=sys.stderr)
                time.sleep(5 * attempt)
        parts.append(part)
    return parts


def extract(parts: list[Path], cache: Path) -> Path:
    """Extract every batch into one folder, keeping each batch's MANIFEST apart.

    Each GDC tar carries its own MANIFEST.txt listing only its own members, so
    they are renamed per part before merging; letting them overwrite would lose
    the checksums for every earlier batch.
    """
    folder = cache / "extract"
    marker = folder / ".complete"
    if marker.is_file():
        return folder
    folder.mkdir(parents=True, exist_ok=True)
    for part in parts:
        label = part.name.split(".")[0]
        with tarfile.open(part, "r:gz") as tar:
            for member in tar.getmembers():
                name = Path(member.name)
                if name.is_absolute() or ".." in name.parts or not member.isfile():
                    continue
                if name.name == "MANIFEST.txt":
                    member.name = f"MANIFEST-{label}.txt"
                tar.extract(member, folder, filter="data")
    marker.write_text("ok\n")
    return folder


def merged_manifest(folder: Path) -> dict:
    """All per-batch MANIFEST rows keyed by file id."""
    declared = {}
    for path in sorted(folder.glob("MANIFEST*.txt")):
        for line in path.read_text(encoding="utf-8").splitlines()[1:]:
            if not line.strip():
                continue
            file_id, filename, md5sum, size, state = line.split("\t")
            if file_id in declared and declared[file_id]["md5"] != md5sum:
                raise SystemExit(f"conflicting MANIFEST rows for {file_id}")
            declared[file_id] = {"filename": filename, "md5": md5sum,
                                 "bytes": int(size), "state": state}
    return declared


def verify(folder: Path, records: list[dict]) -> dict:
    """Check every declared file against GDC's own MANIFEST, then the API md5."""
    declared = merged_manifest(folder)
    if not declared:
        raise SystemExit("bundle has no MANIFEST.txt")
    missing = [r["file_id"] for r in records if r["file_id"] not in declared]
    if missing:
        raise SystemExit(f"bundle is missing {len(missing)} declared files: {missing[:5]}")
    problems = []
    for record in records:
        item = declared[record["file_id"]]
        path = folder / item["filename"]
        if not path.is_file():
            problems.append(f"{record['file_id']}: file absent")
            continue
        if path.stat().st_size != item["bytes"] or item["bytes"] != record["bytes"]:
            problems.append(f"{record['file_id']}: size {path.stat().st_size} != {item['bytes']}")
        if md5(path) != item["md5"]:
            problems.append(f"{record['file_id']}: md5 differs from MANIFEST")
        if item["md5"] != record["md5"]:
            problems.append(f"{record['file_id']}: MANIFEST md5 differs from API md5")
    if problems:
        raise SystemExit("checksum verification failed:\n  " + "\n  ".join(problems[:20]))
    return declared


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--verify-only", action="store_true",
                        help="Verify the cached bundle without contacting GDC")
    parser.add_argument("--resume", action="store_true",
                        help="Reuse completed batches and an existing bundle")
    args = parser.parse_args(argv)
    root = args.project_root.resolve()
    cache = root / CACHE
    cache.mkdir(parents=True, exist_ok=True)

    report_path = cache / REPORT
    if args.verify_only:
        if not report_path.is_file():
            raise SystemExit("no fetch_report.json; run without --verify-only first")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        parts = sorted(cache.glob("part-*.tar.gz"))
        folder = extract(parts, cache)
        verify(folder, report["files"])
        print(json.dumps({"status": "passed", "files": len(report["files"]),
                          "data_release": report["data_release"]}, indent=2))
        return 0

    status = release_status()
    records = files()
    print(f"{len(records)} files; {status['data_release']}")
    parts = fetch_parts(cache, records)
    folder = extract(parts, cache)
    verify(folder, records)
    report = {"status": "complete", **status, "project_id": PROJECT,
              "data_type": DATA_TYPE, "access": ACCESS,
              "workflow": sorted({r["workflow"] for r in records}),
              "bundle": [str(p.relative_to(root)) for p in parts],
              "bundle_bytes": sum(p.stat().st_size for p in parts),
              "files": records}
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "passed", "files": len(records),
                      "bundle_bytes": report["bundle_bytes"],
                      "data_release": status["data_release"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

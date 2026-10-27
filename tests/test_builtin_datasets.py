"""Built-in dataset structure, manifest integrity and workspace seeding.

The shipped TCGA-LIHC files are read-only fixtures here: the tests verify what the
manifest claims about them and never rewrite or rebuild the data.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1]
DATASETS = SOURCE / "datasets"
DATASET = DATASETS / "TCGA-LIHC"
MANIFEST = DATASET / "dataset.json"


def manifest():
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def read_csv(path):
    import csv
    with Path(path).open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_manifest_declares_every_required_field():
    value = manifest()
    for key in ("schema_version", "dataset_id", "title", "provenance_status", "source",
                "license", "upstream_files", "files", "samples", "groups", "bindings",
                "quality_flags", "build"):
        assert key in value, key
    assert value["schema_version"] == 1
    assert value["dataset_id"] == DATASET.name
    # The dataset ships from GDC, so the provenance must be recorded, not deferred.
    assert value["provenance_status"] == "recorded"
    source = value["source"]
    assert source["project_id"] == "TCGA-LIHC"
    assert source["access_tier"] == "open"
    assert "gdc" in source["repository"].lower()
    assert "STAR" in source["upstream_pipeline"]
    assert source["gene_model"] == "GENCODE v36"
    assert source["expression_columns"] == {"counts": "unstranded", "tpm": "tpm_unstranded"}
    assert source["data_release"] and source["retrieved_at"]
    assert source["gdc_commit"]
    assert "GPL-3.0-or-later does not cover" in value["license"]["code_license"]
    assert value["license"]["data_terms"] != "not_recorded"
    # Every shipped sample must be traceable to the GDC file it came from.
    assert len(value["upstream_files"]) == 424
    for item in value["upstream_files"]:
        assert item["file_id"] and item["sample_id"] and item["md5"]
        assert item["bytes"] > 0


def test_declared_files_match_disk_bytes_and_checksums():
    value = manifest()
    for item in value["files"]:
        path = DATASET / item["path"]
        data = path.read_bytes()
        assert len(data) == item["bytes"], item["path"]
        assert hashlib.sha256(data).hexdigest() == item["sha256"], item["path"]


def test_expression_manifest_records_units_and_dimensions():
    """The manifest must state what the numbers are, not only how big the matrix is."""
    value = manifest()
    expression = {item["path"]: item for item in value["files"]
                  if item["path"].startswith("expression/")}
    assert set(expression) == {"expression/counts_full.rds", "expression/counts_paired.rds",
                              "expression/tpm_full.rds", "expression/gene_annotation.csv"}
    full, paired, tpm = (expression["expression/counts_full.rds"],
                         expression["expression/counts_paired.rds"],
                         expression["expression/tpm_full.rds"])
    assert (full["rows"], full["columns"]) == (19938, 424)
    assert (paired["rows"], paired["columns"]) == (19938, 100)
    assert (tpm["rows"], tpm["columns"]) == (19938, 424)
    assert full["dtype"] == paired["dtype"] == "integer"
    assert tpm["dtype"] == "numeric"
    assert full["preprocessing"] == "none"
    # The shipped scope is protein_coding only, so the TPM column sums are below the
    # full-transcriptome 1e6; that fact must be stated, never rescaled away.
    assert "1e6" in tpm["units"]
    assert any("protein_coding" in flag for flag in value["quality_flags"])
    assert all(item["axes"]["rows"].startswith("gene symbol") for item in expression.values())
    # One representative gene_id per matrix row, so users can filter by gene_type.
    annotation = expression["expression/gene_annotation.csv"]
    assert annotation["rows"] == 19938
    assert annotation["columns"] == 3


@pytest.mark.skipif(shutil.which("Rscript") is None, reason="matrix structure needs Rscript")
def test_rds_matrices_are_named_and_paired_columns_follow_metadata(tmp_path):
    """The R side is the authority: dimnames must exist and paired order must match."""
    script = tmp_path / "check.R"
    script.write_text(
        "e <- commandArgs(TRUE)[1]\n"
        "c <- readRDS(file.path(e, 'expression/counts_full.rds'))\n"
        "p <- readRDS(file.path(e, 'expression/counts_paired.rds'))\n"
        "t <- readRDS(file.path(e, 'expression/tpm_full.rds'))\n"
        "s <- utils::read.csv(file.path(e, 'metadata/samples_paired.csv'), colClasses='character')\n"
        "f <- utils::read.csv(file.path(e, 'metadata/samples_full.csv'), colClasses='character')\n"
        "stopifnot(is.matrix(c), is.matrix(p), is.matrix(t))\n"
        "stopifnot(identical(dim(c), c(19938L, 424L)), identical(dim(p), c(19938L, 100L)))\n"
        "stopifnot(identical(dim(t), c(19938L, 424L)))\n"
        "stopifnot(!is.null(rownames(c)), !is.null(colnames(c)))\n"
        "stopifnot(identical(colnames(p), s$sample_id))\n"
        "stopifnot(identical(colnames(c), f$sample_id))\n"
        "stopifnot(setequal(rownames(c), rownames(t)), !anyDuplicated(rownames(c)))\n"
        "stopifnot(is.integer(c[1, 1]), all(c >= 0), all(is.finite(t)), all(t >= 0))\n"
        "stopifnot(all(rowSums(c != 0) >= 0))\n"
        "cat('ok\\n')\n",
        encoding="utf-8")
    import subprocess
    result = subprocess.run(["Rscript", "--vanilla", str(script), str(DATASET)],
                            capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stderr[-2000:]
    assert "ok" in result.stdout


def test_duplicate_gene_symbols_are_summed_not_dropped(tmp_path):
    """GENCODE v36 repeats some symbols; both rows must contribute to the matrix row.

    The previous hand-made dataset kept only the first row per symbol, which lost
    up to 27 counts for MATR3/PDE11A/POLR2J3/TMSB15B. The build must sum them.
    """
    script = tmp_path / "dup.R"
    script.write_text(
        "e <- commandArgs(TRUE)[1]\n"
        "c <- readRDS(file.path(e, 'expression/counts_full.rds'))\n"
        "t <- readRDS(file.path(e, 'expression/tpm_full.rds'))\n"
        "a <- utils::read.csv(file.path(e, 'expression/gene_annotation.csv'), colClasses='character')\n"
        "stopifnot(identical(a$gene_name, rownames(c)))\n"
        "stopifnot(all(a$gene_type == 'protein_coding'))\n"
        "stopifnot(!anyDuplicated(a$gene_name), all(nzchar(a$gene_id)))\n"
        "stopifnot(identical(colnames(c), colnames(t)))\n"
        "cat('ok\\n')\n",
        encoding="utf-8")
    import subprocess
    result = subprocess.run(["Rscript", "--vanilla", str(script), str(DATASET)],
                            capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stderr[-2000:]
    assert "ok" in result.stdout


def test_manifest_documents_the_duplicate_symbol_rule():
    value = manifest()
    rule = value["source"]["duplicate_symbol_rule"]
    assert "summed" in rule.lower()
    flag = next((f for f in value["quality_flags"] if "more than once" in f), None)
    assert flag is not None, "the duplicate-symbol policy must be a quality flag"
    # The rule must name the symbols so a reader can audit them.
    for symbol in ("MATR3", "POLR2J3", "CD99"):
        assert symbol in flag
    full = read_csv(DATASET / "metadata/samples_full.csv")
    paired = read_csv(DATASET / "metadata/samples_paired.csv")
    assert len(full) == 424 and len(paired) == 100
    groups = {}
    for row in full:
        groups[row["group"]] = groups.get(row["group"], 0) + 1
        assert row["group"] and row["sample_id"]
    assert groups == {"Tumor": 371, "Non_Tumor": 50, "Recurrent_Tumor": 3}
    assert len({row["sample_id"] for row in full}) == 424
    pair_ids = [row["pair_id"] for row in paired]
    assert all(pair_ids) and len({(row["pair_id"], row["group"]) for row in paired}) == 100
    per_pair = {}
    for row in paired:
        per_pair.setdefault(row["pair_id"], set()).add(row["group"])
    assert len(per_pair) == 50
    assert all(levels == {"Tumor", "Non_Tumor"} for levels in per_pair.values())
    # Only paired patients carry a pair_id in the full table; the rest are "NA".
    assert sum(1 for row in full if row["pair_id"] not in {"", "NA"}) == 100


def test_sample_ids_are_sixteen_character_gdc_barcodes():
    """GDC keys files by 16-character sample barcodes; aliquot suffixes must not leak in."""
    for name in ("metadata/samples_full.csv", "metadata/samples_paired.csv"):
        rows = read_csv(DATASET / name)
        for row in rows:
            sample = row["sample_id"]
            assert len(sample) == 16, (name, sample)
            assert sample.startswith("TCGA-")
            assert row["patient"] == sample[:12]
            assert row["file_id"], (name, sample)
    # A recurrent patient with two recurrent aliquots keeps both distinct samples.
    full = read_csv(DATASET / "metadata/samples_full.csv")
    aaca = [r["sample_id"] for r in full if r["patient"] == "TCGA-DD-AACA"]
    assert len(aaca) == 3 and len(set(aaca)) == 3


def test_no_excel_corrupted_clinical_values_are_shipped():
    """The previous hand-made table had `morphology` mangled into March-1970 dates."""
    for name in ("metadata/samples_full.csv", "metadata/samples_paired.csv"):
        text = (DATASET / name).read_text(encoding="utf-8")
        assert "Mar-70" not in text and "Mar-80" not in text
    # Only GDC sample-level fields are shipped; no diagnosis table is bundled.
    assert not (DATASET / "metadata/clinical_gdc_full.csv").exists()
    header = (DATASET / "metadata/samples_full.csv").read_text(encoding="utf-8").splitlines()[0]
    assert "morphology" not in header
    assert "sample_type" in header and "tissue_type" in header


def test_manifest_counts_match_the_metadata_tables():
    value = manifest()
    full = read_csv(DATASET / "metadata/samples_full.csv")
    samples = value["samples"]
    assert samples["total"] == len(full)
    assert samples["tumor"] == sum(1 for r in full if r["group"] == "Tumor")
    assert samples["normal"] == sum(1 for r in full if r["group"] == "Non_Tumor")
    assert samples["recurrent"] == sum(1 for r in full if r["group"] == "Recurrent_Tumor")
    assert samples["paired_patients"] == len(
        {r["pair_id"] for r in read_csv(DATASET / "metadata/samples_paired.csv")})


def test_bindings_point_at_seeded_paths_and_cover_group_levels():
    value = manifest()
    # Seed target is data/metadata/<id>/<name>; the shipped copy is metadata/<name>.
    declared = {item["path"] for item in value["files"]}
    for name, binding in value["bindings"].items():
        assert binding["path"].startswith("data/raw/TCGA-LIHC/"), name
        assert binding["metadata_path"].startswith("data/metadata/TCGA-LIHC/"), name
        assert binding["sample_axis"] == "columns"
        assert binding["sample_key"] == "sample_id"
        assert binding["preprocessing"] == "none"
        seeded = binding["metadata_path"].split("TCGA-LIHC/", 1)[1]
        assert "metadata/" + seeded in declared, name
        table = read_csv(DATASET / "metadata" / seeded)
        levels = {row[binding["group_key"]] for row in table}
        assert set(binding["group_map"]) == levels, name
        # Every mapped key is also a declared level, never an invented label.
        assert set(binding["group_map"].values()) <= levels, name
    assert value["bindings"]["counts_paired"]["pair_key"] == "pair_id"
    assert value["bindings"]["counts_full"]["pair_key"] is None
    assert value["bindings"]["tpm_full"]["pair_key"] is None


def test_upstream_files_record_the_gdc_checksums():
    """GDC declares md5 per file; the manifest must carry it, not a local-only path."""
    value = manifest()
    for item in value["upstream_files"]:
        assert len(item["md5"]) == 32 and all(c in "0123456789abcdef" for c in item["md5"])
        assert item["sample_id"].startswith("TCGA-")
        assert item["role"] in {"Primary Tumor", "Solid Tissue Normal", "Recurrent Tumor"}
    assert len({item["file_id"] for item in value["upstream_files"]}) == 424
    assert len({item["sample_id"] for item in value["upstream_files"]}) == 424


def test_catalog_reports_the_dataset_with_recorded_provenance():
    from multiomics_tui.datasets import catalog, provenance_pending, seeds
    items = catalog(SOURCE)
    assert [item["manifest"]["dataset_id"] for item in items] == ["TCGA-LIHC"]
    # Provenance is complete, so nothing blocks a release.
    assert provenance_pending(SOURCE) == []
    mapping = seeds(SOURCE)
    assert len(mapping) == len(manifest()["files"])
    assert all(target.startswith(("data/raw/TCGA-LIHC/", "data/metadata/TCGA-LIHC/"))
               for target in mapping)


def test_catalog_is_empty_without_a_datasets_directory(tmp_path, monkeypatch):
    from multiomics_tui import datasets
    monkeypatch.setattr(datasets, "datasets_root", lambda workspace: tmp_path / "missing")
    assert datasets.catalog(tmp_path) == []
    assert datasets.seeds(tmp_path) == {}
    assert datasets.provenance_pending(tmp_path) == []


def test_catalog_rejects_a_file_whose_bytes_changed(tmp_path, monkeypatch):
    from multiomics_tui import datasets
    copy = tmp_path / "datasets"
    shutil.copytree(DATASET, copy / "TCGA-LIHC")
    (copy / "TCGA-LIHC/expression/counts_paired.rds").write_bytes(b"not the frozen matrix")
    monkeypatch.setattr(datasets, "datasets_root", lambda workspace: copy)
    with pytest.raises(datasets.DatasetError, match="size differs|checksum differs"):
        datasets.catalog(tmp_path)


def test_summary_never_prints_sample_identifiers():
    from multiomics_tui.datasets import summary_lines
    text = "\n".join(summary_lines(SOURCE, "zh"))
    assert "TCGA-LIHC" in text
    assert "counts_paired" in text
    assert "total=424" in text
    assert "TCGA-5R-AA1C" not in text


def test_workspace_initialize_seeds_datasets_without_overwriting(tmp_path):
    from multiomics_tui.workspace import initialize
    target = tmp_path / "study"
    target.mkdir()
    created = initialize(target, SOURCE)
    seeded = [name for name in created if name.startswith(("data/raw/TCGA-LIHC/",
                                                           "data/metadata/TCGA-LIHC/"))]
    assert len(seeded) == len(manifest()["files"])
    for relative in seeded:
        assert (target / relative).is_file()
    # A second run must not rewrite anything, and a user edit must survive.
    edited = target / "data/metadata/TCGA-LIHC/samples_paired.csv"
    edited.write_text("sample_id\nuser-edit\n", encoding="utf-8")
    again = initialize(target, SOURCE)
    assert not any(name.startswith(("data/raw/TCGA-LIHC/", "data/metadata/TCGA-LIHC/"))
                   for name in again)
    assert edited.read_text(encoding="utf-8") == "sample_id\nuser-edit\n"


def test_workspace_initialize_skips_datasets_when_the_source_tree_has_none(tmp_path, monkeypatch):
    from multiomics_tui import datasets, workspace
    monkeypatch.setattr(datasets, "datasets_root", lambda root: tmp_path / "absent")
    target = tmp_path / "study"
    target.mkdir()
    created = workspace.initialize(target, SOURCE)
    assert not any(name.startswith("data/raw/TCGA-LIHC/") for name in created)
    assert (target / "config/study_config.yaml").is_file()


def test_cd_seeds_the_dataset_into_the_new_workspace(project_root, stub_cfg, tmp_path):
    """``/cd`` must leave the dataset ready to bind, not just create directories."""
    from multiomics_tui.controller import Session
    session = Session(project_root, cfg=stub_cfg, client=None, app_root=SOURCE,
                      probe=lambda target: None)
    target = tmp_path / "study"
    target.mkdir()
    response = session.switch_root(str(target))
    assert response.kind == "cwd"
    assert (target / "data/raw/TCGA-LIHC/counts_paired.rds").is_file()
    assert (target / "data/metadata/TCGA-LIHC/samples_paired.csv").is_file()
    assert "data/raw/TCGA-LIHC/counts_paired.rds" in response.message
    # The seeded copy matches the frozen bytes, so the manifest bindings hold.
    assert (target / "data/raw/TCGA-LIHC/counts_paired.rds").read_bytes() == \
        (DATASET / "expression/counts_paired.rds").read_bytes()


def test_public_config_initialize_seeds_datasets_once(tmp_path, monkeypatch):
    from native_dist import public_config
    from multiomics_tui import datasets
    target = tmp_path / "installed"
    target.mkdir()
    for name in ("config/study_config.example.yaml", "examples/synthetic_expression.csv",
                 "examples/synthetic_samples.csv"):
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((SOURCE / name).read_bytes())
    monkeypatch.setattr(datasets, "datasets_root", lambda root: DATASET.parent)
    created = public_config.initialize(target)
    seeded = [name for name in created if name.startswith("data/raw/TCGA-LIHC/")]
    assert seeded
    assert (target / "data/raw/TCGA-LIHC/counts_paired.rds").is_file()
    assert public_config.initialize(target) == []


def test_datasets_command_lists_the_dataset_read_only(project_root, stub_cfg):
    from multiomics_tui.controller import READONLY_COMMANDS, Session
    assert "/datasets" in READONLY_COMMANDS
    session = Session(project_root, cfg=stub_cfg, client=None, app_root=SOURCE)
    response = session.process_view_command("/datasets")
    assert response.kind == "text"
    assert "TCGA-LIHC" in response.message
    assert "counts_paired" in response.message
    assert "TCGA-5R-AA1C" not in response.message
    busy = session.readonly_command("/datasets")
    assert busy.kind == "text" and busy.read_only


def test_release_audit_sees_the_dataset_and_clears_provenance():
    from native_dist import public_release
    assert [item["dataset_id"] for item in public_release.builtin_datasets(SOURCE)] == ["TCGA-LIHC"]
    # The release gate must pass now that the source and terms are recorded.
    assert public_release.pending_provenance(SOURCE) == []

"""A08: rds_table must enforce the same declared contract semantics as CSV."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from multiomics_tui.output_contracts import validate_contracts, verify_output

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "multiomics_tui/output_probe.R"
RSCRIPT = shutil.which("Rscript")


def project_libraries() -> list[str]:
    libraries = []
    local = ROOT / "config/runtime.local.yaml"
    if local.is_file():
        try:
            import yaml
            runtime = (yaml.safe_load(local.read_text(encoding="utf-8")) or {}).get("runtime") or {}
            libraries.extend(str(p) for p in runtime.get("r_library_paths") or [] if Path(p).is_dir())
        except (OSError, ValueError):
            pass
    system = subprocess.run([RSCRIPT, "--vanilla", "-e", 'cat(.libPaths(), sep="\\n")'],
                            capture_output=True, text=True, timeout=60,
                            env={**os.environ, "R_LIBS_USER": os.pathsep.join(libraries)})
    if system.returncode == 0:
        libraries.extend(line.strip() for line in system.stdout.splitlines() if Path(line.strip()).is_dir())
    return list(dict.fromkeys(libraries))


def r_environment() -> dict:
    return {**os.environ, "R_LIBS_USER": os.pathsep.join(project_libraries())}


def make_rds(path: Path, data: dict) -> None:
    assignments = []
    for name, values in data.items():
        literal = ", ".join("NA" if value is None else
                            ("Inf" if value == "Inf" else json.dumps(value))
                            for value in values)
        assignments.append(f"{name}=c({literal})")
    script = f"saveRDS(data.frame({', '.join(assignments)}, stringsAsFactors=FALSE), commandArgs(TRUE)[1])"
    subprocess.run([RSCRIPT, "--vanilla", "-e", script, str(path)], check=True,
                   capture_output=True, env=r_environment(), timeout=120)


def run_probe(tmp_path: Path, rds_path: Path, contract: dict, binding=None):
    folder = tmp_path / ("probe_" + str(abs(hash(json.dumps(contract, sort_keys=True)))))
    folder.mkdir(parents=True, exist_ok=True)
    request = folder / "request.json"
    result = folder / "result.json"
    payload = {"path": str(rds_path), "contract": contract}
    if binding is not None:
        payload["binding"] = binding
    request.write_text(json.dumps(payload), encoding="utf-8")
    process = subprocess.run([RSCRIPT, "--vanilla", str(PROBE), str(request), str(result)],
                             capture_output=True, text=True, env=r_environment(), timeout=120)
    return process, result


def test_rds_table_rejects_missing_duplicate_and_nonfinite(tmp_path):
    if RSCRIPT is None:
        pytest.skip("Rscript is unavailable")
    cases = [
        ({"id": [None, None], "value": ["Inf", None]},
         {"path": "objects/x.rds", "format": "rds_table", "columns": ["id", "value"],
          "unique_by": ["id"], "nonmissing": ["id"], "finite": ["value"]},
         "Missing required output value"),
        ({"id": ["A", "A"], "value": [1, 2]},
         {"path": "objects/x.rds", "format": "rds_table", "columns": ["id", "value"],
          "unique_by": ["id"], "nonmissing": ["id"], "finite": ["value"]},
         "Duplicate output key"),
        ({"id": ["A", "B"], "value": [1, "Inf"]},
         {"path": "objects/x.rds", "format": "rds_table", "columns": ["id", "value"],
          "unique_by": ["id"], "nonmissing": ["id"], "finite": ["value"]},
         "Nonfinite output"),
    ]
    for index, (data, contract, message) in enumerate(cases):
        path = tmp_path / f"invalid_{index}.rds"
        make_rds(path, data)
        process, _ = run_probe(tmp_path / str(index), path, contract)
        assert process.returncode != 0
        assert message in process.stderr


def test_rds_table_accepts_a_valid_frame(tmp_path):
    if RSCRIPT is None:
        pytest.skip("Rscript is unavailable")
    path = tmp_path / "valid.rds"
    make_rds(path, {"id": ["A", "B"], "value": [1.5, 2.5]})
    contract = {"path": "objects/valid.rds", "format": "rds_table",
                "columns": ["id", "value"], "unique_by": ["id"],
                "nonmissing": ["id"], "finite": ["value"]}
    process, result = run_probe(tmp_path, path, contract)
    assert process.returncode == 0, process.stderr
    payload = json.loads(result.read_text(encoding="utf-8"))
    assert payload["status"] == "passed" and payload["dimensions"] == [2, 2]


def test_rds_table_binding_checks_samples_groups_and_pairs(tmp_path):
    if RSCRIPT is None:
        pytest.skip("Rscript is unavailable")
    path = tmp_path / "bound.rds"
    make_rds(path, {"sample_id": ["S1", "S2"], "group": ["Tumor", "Non_Tumor"],
                    "pair": ["P1", "P1"], "value": [1.0, 2.0]})
    contract = {"path": "objects/bound.rds", "format": "rds_table",
                "columns": ["sample_id", "group", "pair", "value"],
                "binding_index": 0, "sample_key": "sample_id",
                "group_key": "group", "pair_key": "pair", "finite": ["value"]}
    def sample_hash(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()
    def pair_hash(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()
    binding = {sample_hash("S1"): ["Tumor", pair_hash("P1")],
               sample_hash("S2"): ["Non_Tumor", pair_hash("P1")]}
    process, result = run_probe(tmp_path / "ok", path, contract, binding)
    assert process.returncode == 0, process.stderr
    assert json.loads(result.read_text(encoding="utf-8"))["status"] == "passed"

    wrong_group = json.loads(json.dumps(binding))
    wrong_group[sample_hash("S1")][0] = "Non_Tumor"
    process, _ = run_probe(tmp_path / "group", path, contract, wrong_group)
    assert process.returncode != 0 and "group disagrees" in process.stderr

    wrong_pair = json.loads(json.dumps(binding))
    wrong_pair[sample_hash("S1")][1] = pair_hash("P2")
    process, _ = run_probe(tmp_path / "pair", path, contract, wrong_pair)
    assert process.returncode != 0 and "pairing disagrees" in process.stderr

    dropped_path = tmp_path / "dropped.rds"
    make_rds(dropped_path, {"sample_id": ["S1"], "group": ["Tumor"],
                            "pair": ["P1"], "value": [1.0]})
    process, _ = run_probe(tmp_path / "dropped", dropped_path, contract, binding)
    assert process.returncode != 0 and "dropped samples" in process.stderr

    unexpected_path = tmp_path / "unexpected.rds"
    make_rds(unexpected_path, {"sample_id": ["S1", "S2", "S3"], "group": ["Tumor", "Non_Tumor", "Tumor"],
                               "pair": ["P1", "P1", "P2"], "value": [1.0, 2.0, 3.0]})
    process, _ = run_probe(tmp_path / "unexpected", unexpected_path, contract, binding)
    assert process.returncode != 0 and "Unexpected output sample" in process.stderr


def test_rds_table_binding_requires_sample_key_at_planning():
    with pytest.raises(ValueError, match="Bound sample tables require sample_key"):
        validate_contracts([{"path": "objects/x.rds", "format": "rds_table",
                             "columns": ["id", "value"], "binding_index": 0}],
                           {"outputs": ["objects/x.rds"]})
    validate_contracts([{"path": "objects/x.rds", "format": "rds_table",
                         "columns": ["sample_id", "value"], "binding_index": 0,
                         "sample_key": "sample_id"}], {"outputs": ["objects/x.rds"]})


def test_csv_and_rds_reject_the_same_missing_values(tmp_path):
    if RSCRIPT is None:
        pytest.skip("Rscript is unavailable")
    contract = {"path": "tables/x.csv", "format": "csv", "columns": ["id", "value"],
                "unique_by": ["id"], "nonmissing": ["id"], "finite": ["value"]}
    (tmp_path / "tables").mkdir()
    (tmp_path / "tables/x.csv").write_text("id,value\n,Inf\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Missing required output value"):
        verify_output(tmp_path, tmp_path, "tables/x.csv", contract, {}, {})
    path = tmp_path / "invalid.rds"
    make_rds(path, {"id": [None], "value": ["Inf"]})
    rds_contract = dict(contract, path="objects/x.rds", format="rds_table")
    process, _ = run_probe(tmp_path / "rds", path, rds_contract)
    assert process.returncode != 0 and "Missing required output value" in process.stderr

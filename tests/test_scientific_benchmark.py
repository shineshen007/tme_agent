"""Benchmark wiring: engineering cases pass; scientific validation stays NOT_EVALUABLE."""
import json
import subprocess
import sys
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1]


def test_benchmark_runs_and_keeps_scientific_status_not_evaluable(tmp_path):
    result = subprocess.run(
        [sys.executable, str(SOURCE / "scripts/90_scientific_benchmark.py"),
         "--output", str(tmp_path / "benchmark")],
        cwd=SOURCE, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]
    report = json.loads((tmp_path / "benchmark/scientific_benchmark.json").read_text(encoding="utf-8"))
    assert report["engineering_status"] == "passed"
    assert report["scientific_validation_status"] == "NOT_EVALUABLE"
    assert all(case["status"] == "passed" for case in report["cases"])
    real = {case["case"]: case["status"] for case in report["real_data"]}
    assert real["real_tcga_lihc_structure"] == "complete_structural_only"
    assert real["real_model_reasoning"] == "NOT_EVALUABLE"
    assert real["real_multiomics_integration"] == "NOT_EVALUABLE"


def test_benchmark_does_not_claim_unit_tests_are_science_validation():
    text = (SOURCE / "scripts/90_scientific_benchmark.py").read_text(encoding="utf-8")
    assert "scientific_validation_status" in text
    assert "NOT_EVALUABLE" in text
    assert "not model or clinical validation" in text

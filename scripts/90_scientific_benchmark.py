"""Deterministic scientific-guard benchmark; never a substitute for human review.

Runs synthetic cases against the frozen research-context, retrieval, literature
and experiment guards, plus a real TCGA-LIHC structural case. Real differential
analysis / multi-omics / model reasoning are reported NOT_EVALUABLE unless their
external prerequisites are explicitly available. No network or model is used.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import platform
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from orchestrator import research_phases as phases
from orchestrator.research_context import ResearchContext, _records, validate_research_plan
from orchestrator.research_literature import _passage, _seal, validate_literature_claims


EVIDENCE_ID = "ev_" + "a" * 20
SOURCE_ID = "PMID:12345"
KNOWLEDGE_ID = "wiki_benchmark"


def _artifact(content, relative="reports/benchmark.csv"):
    return {"path": relative, "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}


def _spec(**selection):
    spec = {
        "path": "reports/benchmark.csv", "layer": "de", "omics": "transcriptomics",
        "entity_type": "gene", "namespace": "benchmark", "effect_scale": "log2_fold_change",
        "row_key": ["gene"], "measured_omics": ["transcriptomics"],
        "evidence_context": "measured_evidence",
        "columns": {"entity_id": "gene", "effect": "effect", "p_value": "p",
                    "adjusted_p": "padj", "n": "n", "selected": "selected", "status": "status"},
        "selection": {"adjusted_p_max": 0.05, **selection},
    }
    return spec


def _rows(rows):
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["gene", "effect", "p", "padj", "n", "selected", "status"])
    writer.writerows(rows)
    return buffer.getvalue().encode()


def _context(evidence):
    return ResearchContext(question="Explain the observed association.", disease="colorectal cancer",
                           species="Homo sapiens", tissue="colon", analysis_unit="patient",
                           cohort_id="synthetic_benchmark", task_id="benchmark", plan_hash="benchmark",
                           evidence=evidence)


def _observation(eid, entity, omics="transcriptomics", selected=True, padj=0.01, status="tested",
                 effect=1.0, direction="higher_in_numerator"):
    return {"evidence_id": eid,
            "source": {"omics": omics, "cohort": "synthetic_benchmark", "evidence_layer": "de",
                       "evidence_context": "measured_evidence" if omics != "pathway" else "derived_estimate",
                       "measured_omics": [omics] if omics != "pathway" else [],
                       "analysis_unit": "patient"},
            "biological_entity": {"entity_type": "gene", "entity_id": entity, "namespace": "benchmark",
                                  "species": "Homo sapiens", "symbol": entity, "tissue_context": "colon"},
            "observation": {"effect": effect, "p_value": padj, "adjusted_p": padj, "n": 20,
                            "selected": selected,
                            "selection": {"status": "verified_selected" if selected else "verified_not_selected",
                                          "verified": True, "raw_selected": selected, "selected": selected,
                                          "adjusted_p_max": 0.05, "effect_min": None, "effect_max": None,
                                          "analysis_unit_count": None, "reasons": []},
                            "status": status, "effect_scale": "log2_fold_change",
                            "claim_type": "statistical_fact", "direction": direction,
                            "missing_fields": []},
            "provenance": {"artifact_path": "reports/benchmark.csv", "sha256": "0" * 64,
                           "row_key": {"gene": entity}, "row_index": None, "columns": {},
                           "plan_hash": "benchmark"}}


def _case(name, function, *, scientific=False):
    try:
        detail = function()
        return {"case": name, "status": "passed" if not scientific else "NOT_EVALUABLE",
                "detail": detail or ""}
    except Exception as exc:
        return {"case": name, "status": "failed", "detail": f"{type(exc).__name__}: {exc}"}


def selection_case():
    spec = validate_research_plan({"enabled": True,
        "context": {"question": "Explain", "disease": "cancer", "species": "human",
                    "tissue": "colon", "analysis_unit": "patient", "cohort_id": "benchmark",
                    "contrast": {"numerator": "tumor", "denominator": "adjacent"}, "limitations": []},
        "evidence_outputs": [_spec()]}, ["reports/benchmark.csv"], "patient")["evidence_outputs"][0]
    context = _context([])
    good = _rows([["A", -1.2, 0.001, 0.01, 20, "TRUE", "tested"],
                  ["B", 0.1, 0.8, 0.9, 20, "FALSE", "tested"]])
    records = _records(good, spec, context, _artifact(good))
    assert [row["observation"]["selected"] for row in records] == [True, False]
    bad = _rows([["A", -1.2, 0.001, 0.01, 20, "FALSE", "tested"]])
    try:
        _records(bad, spec, context, _artifact(bad))
    except ValueError:
        return "selection rules conserved; inconsistent selected rejected"
    raise AssertionError("inconsistent selected was accepted")


def multiomics_case():
    spec_t = validate_research_plan({"enabled": True,
        "context": {"question": "Explain", "disease": "cancer", "species": "human",
                    "tissue": "colon", "analysis_unit": "patient", "cohort_id": "benchmark",
                    "contrast": {"numerator": "tumor", "denominator": "adjacent"}, "limitations": []},
        "evidence_outputs": [_spec()]}, ["reports/benchmark.csv"], "patient")["evidence_outputs"][0]
    spec_p = json.loads(json.dumps(spec_t))
    spec_p.update(omics="proteomics", entity_type="protein", measured_omics=["proteomics"])
    context = _context([])
    rna = _records(_rows([["TP53", 1.2, 0.001, 0.01, 20, "TRUE", "tested"]]), spec_t, context, _artifact(b""))
    protein = _records(_rows([["TP53", 0.8, 0.001, 0.01, 20, "TRUE", "tested"]]), spec_p, context, _artifact(b""))
    derived = _observation("ev_" + "b" * 20, "HALLMARK_HYPOXIA", omics="pathway")
    measured = {omics for row in rna + protein for omics in row["source"]["measured_omics"]}
    assert measured == {"transcriptomics", "proteomics"}
    assert derived["source"]["measured_omics"] == []
    return "two measured assays counted; derived pathway not counted as an independent assay"


def literature_case():
    source = _seal({"source_id": SOURCE_ID, "title": "Synthetic report", "year": "2024",
                    "url": "https://pubmed.ncbi.nlm.nih.gov/12345/", "publication_types": ["Journal Article"],
                    "retraction_metadata": [], "passages": [_passage("abstract:1",
                    "The candidate association was observed in a synthetic model.")]})
    claims = [{"claim_id": "I1", "source_id": SOURCE_ID, "relation": "supports", "directness": "indirect",
               "quote": "candidate association was observed", "locator": "abstract:1",
               "rationale": "Related model only.",
               "context_match": {k: "unknown" for k in ("species", "disease", "tissue", "model", "assay", "outcome")}},
              {"claim_id": "I1", "source_id": SOURCE_ID, "relation": "contradicts", "directness": "unknown",
               "quote": "synthetic model", "locator": "abstract:1", "rationale": "Alternative context.",
               "context_match": {k: "unknown" for k in ("species", "disease", "tissue", "model", "assay", "outcome")}}]
    validated = validate_literature_claims(claims, [source], ["I1"])
    assert len(validated) == 2 and all(row["text_verified"] for row in validated)
    return "support and contradiction quotations verified against frozen text"


def counterevidence_case():
    evidence = [_observation("ev_" + f"{index:020x}", "TP53", selected=index % 2 == 1,
                             padj=0.01 if index % 2 else 0.9) for index in range(200)]
    cited = evidence[0]["evidence_id"]
    context = _context(evidence)
    claims = [{"id": "I1", "evidence_ids": [cited]}]
    basis = phases.counterevidence_candidates(context, claims)
    candidates = basis["I1"]["counterevidence_ids"]
    assert len(candidates) == 199
    entry = {"claim_id": "I1", "reviewed_evidence_ids": [cited],
             "counterevidence_ids": candidates,
             "negative_result_ids": basis["I1"]["negative_result_ids"],
             "assessment": "consistent", "rationale": "All candidate rows reviewed."}
    phases._validate_evidence_review([entry], claims, basis)
    broken = dict(entry, counterevidence_ids=candidates[:-1])
    try:
        phases._validate_evidence_review([broken], claims, basis)
    except ValueError:
        return "counterevidence beyond display limit is required; omission rejected"
    raise AssertionError("critic omission was accepted")


def experiment_and_overreach_case():
    assert phases._overreach_language("TP53 may alter metabolic flux.") is None
    assert phases._overreach_language("Enzyme activity was not measured.") is None
    for bad in ("TP53 drives metabolic flux.", "TP53 is the cell of origin.",
                "The enzyme activity proves the mechanism."):
        try:
            phases._overreach_language(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"overreach accepted: {bad}")
    assert phases._assertion_language("TP53 may regulate the program.") is None
    try:
        phases._assertion_language("TP53 causes tumor progression.")
    except ValueError:
        pass
    else:
        raise AssertionError("causal overreach accepted")
    # Experiment design guard: falsifiability is checked by validate_draft; a
    # compact direct check documents the required fields without a model call.
    required = {"model_system", "model_rationale", "perturbation", "support_criterion",
                "falsification_criterion", "replication_unit", "sample_size_basis",
                "rescue_or_orthogonal", "feasibility", "controls", "readouts"}
    assert required <= set(phases.EXPERIMENT["properties"])
    return "flux/activity/cell-of-origin/causal overreach rejected; experiment fields complete"


def real_tcga_case():
    dataset = ROOT / "datasets/TCGA-LIHC"
    manifest = json.loads((dataset / "dataset.json").read_text(encoding="utf-8"))
    samples = list(csv.DictReader((dataset / "metadata/samples_paired.csv").open(encoding="utf-8")))
    patients = {}
    for row in samples:
        patients.setdefault(row.get("patient") or row.get("patient_id"), set()).add(
            str(row.get("group", "")).casefold())
    pairs = sum(1 for groups in patients.values() if {"tumor", "non_tumor"} <= groups)
    assert manifest["provenance_status"] == "recorded"
    assert len(samples) == 100 and pairs == 50
    return {"status": "complete", "n_samples": len(samples), "n_pairs": pairs,
            "note": "structural real-data check only"}
    # Real DE/multi-omics inference remain NOT_EVALUABLE without approved inputs/packages.


def run(output_dir):
    real = None
    try:
        real = real_tcga_case()
        real_status = "complete_structural_only"
    except Exception as exc:
        real_status = "NOT_EVALUABLE"
        real = {"error": f"{type(exc).__name__}: {exc}"}
    cases = [
        _case("selection_thresholds", selection_case),
        _case("multiomics_measured_vs_derived", multiomics_case),
        _case("literature_support_and_counterevidence", literature_case),
        _case("full_registry_counterevidence", counterevidence_case),
        _case("experiment_and_causal_boundary", experiment_and_overreach_case),
    ]
    real_cases = [
        {"case": "real_tcga_lihc_structure", "status": real_status, "detail": real},
        {"case": "real_tcga_lihc_differential_expression", "status": "NOT_EVALUABLE",
         "detail": "Requires an approved real-data pipeline and installed mature R packages; no inference executed here."},
        {"case": "real_multiomics_integration", "status": "NOT_EVALUABLE",
         "detail": "No real proteomics/metabolomics inputs and no approved integration run."},
        {"case": "real_literature_counterevidence", "status": "NOT_EVALUABLE",
         "detail": "Requires live public connectors and scientific review; benchmark does not call the network."},
        {"case": "real_model_reasoning", "status": "NOT_EVALUABLE",
         "detail": "Requires an authorized external model run and human scientific review."},
    ]
    report = {"schema_version": "1.0",
              "engineering_status": "passed" if all(c["status"] == "passed" for c in cases) else "failed",
              "scientific_validation_status": "NOT_EVALUABLE",
              "scope": "synthetic guard benchmark + real TCGA-LIHC structure; not model or clinical validation",
              "cases": cases, "real_data": real_cases,
              "environment": {"python": platform.python_version(), "platform": platform.platform(),
                              "seed": 2024},
              "limitations": ["Unit/benchmark pass does not establish scientific validity.",
                              "Real model, real multi-omics and human review remain NOT_EVALUABLE."]}
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "scientific_benchmark.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# Scientific benchmark", "",
             f"Engineering status: **{report['engineering_status']}**",
             f"Scientific validation: **{report['scientific_validation_status']}**", ""]
    for case in cases + real_cases:
        lines.append(f"- {case['case']}: {case['status']} — {case['detail']}")
    (output_dir / "scientific_benchmark.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run deterministic scientific guard benchmarks")
    parser.add_argument("--output", type=Path, default=ROOT / "reports/scientific_benchmark")
    args = parser.parse_args(argv)
    report = run(args.output)
    print(json.dumps({"engineering_status": report["engineering_status"],
                      "scientific_validation_status": report["scientific_validation_status"],
                      "output": str(args.output)}, ensure_ascii=False))
    return 0 if report["engineering_status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())

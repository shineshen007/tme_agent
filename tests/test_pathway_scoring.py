"""Frozen GSVA multi-method pathway scoring: trigger, protocol, plan gate and verifier.

The offline tests fix the deterministic contract (which methods, which threshold, which
outputs, what the verifier rejects). The R tests generate the five tables with real GSVA
and check that the verifier reproduces them; they skip when GSVA is unavailable rather
than pretending the protocol works.
"""
import hashlib
import json
import math
import shutil
import subprocess
from pathlib import Path

import pytest

from multiomics_tui.task_planner import render_plan, validate_plan
from orchestrator import pathway_scoring as scoring
from orchestrator.pathway_scoring import OUTPUTS


CFG = {
    "runtime": {"workers": 1},
    "llm": {"enabled": False, "model": "stub"},
    "analysis": {"pathway_scoring": {
        "trigger": ["gsva", "ssgsea", "plage", "zscore", "通路评分", "通路得分",
                    "基因集评分", "pathway score", "gene set score"],
        "methods": ["ssgsea", "plage", "zscore", "gsva"],
        "adjusted_p_max": 0.05, "consensus_min_methods": 2,
        "min_size": 5, "max_size": 500, "seed": 2024}},
}
REQUEST = ("用 counts_paired 计算 GSVA 四方法（ssgsea、plage、zscore、gsva）通路评分，"
           "按 group 比较肿瘤与癌旁，配对 Wilcoxon，取显著通路得分")
REQUEST_WITH_THRESHOLD = REQUEST + "，FDR 0.05"
GENES = [f"GENE{index}" for index in range(1, 41)]
GENE_SETS = {"SET_A": GENES[0:12], "SET_B": GENES[12:24], "SET_C": GENES[24:32],
             "SET_D": GENES[32:40], "SET_TINY": GENES[0:2]}
MIN_SIZE = 5
SCORED_SETS = [name for name, members in GENE_SETS.items() if len(members) >= MIN_SIZE]
SAMPLES = [f"S{index}" for index in range(1, 9)]
PATIENTS = [f"P{index}" for index in range(1, 5)]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sha(value: str) -> str:
    return hashlib.sha256(str(value).encode()).hexdigest()


def metadata_rows():
    """Four patients, one tumour and one adjacent sample each; ids match the matrix."""
    rows = []
    for index, patient in enumerate(PATIENTS):
        rows.append((SAMPLES[2 * index], patient, "Tumor"))
        rows.append((SAMPLES[2 * index + 1], patient, "Non_Tumor"))
    return rows


def key(values):
    return {"values": [sha(v) for v in values], "missing": 0, "duplicates": 0}


def build_project(tmp_path: Path, *, scores=None) -> Path:
    """Synthetic project with inspected inputs and a trusted inspection audit."""
    root = tmp_path
    for relative in ("data/raw", "data/metadata", "data/signature"):
        (root / relative).mkdir(parents=True, exist_ok=True)
    (root / "config").mkdir()
    (root / "config/study_config.yaml").write_text("schema_version: '1.0'\n", encoding="utf-8")
    expr = root / "data/raw/expr.rds"
    sets = root / "data/signature/sets.rds"
    meta = root / "data/metadata/samples.csv"
    expr.write_bytes(b"synthetic expression placeholder")
    sets.write_bytes(b"synthetic gene-set placeholder")
    rows = metadata_rows()
    meta.write_text("sample_id,patient,group\n" + "".join(
        f"{sample},{patient},{group}\n" for sample, patient, group in rows), encoding="utf-8")
    profiles = [
        {"path": "data/raw/expr.rds", "sha256": digest(expr), "object_class": "matrix",
         "dimensions": [len(GENES), len(SAMPLES)], "numeric_matrix": True, "matrix_like": True,
         "finite": True, "negative_count": 0, "zero_rows": 0, "constant_rows": 2,
         "constant_row_sha256": [sha("GENE1"), sha("GENE2")],
         "keys": {"rows": key(GENES), "columns": key(SAMPLES)}, "columns": {}},
        {"path": "data/metadata/samples.csv", "sha256": digest(meta), "object_class": "data.frame",
         "dimensions": [len(rows), 3], "numeric_matrix": False, "matrix_like": False,
         "keys": {"column:sample_id": key([r[0] for r in rows]),
                  "column:patient": key([r[1] for r in rows]),
                  "column:group": key([r[2] for r in rows])},
         "columns": {"sample_id": {"type": "character", "missing": 0, "unique": len(rows),
                                   "level_hashes": [], "known_levels": []},
                     "patient": {"type": "character", "missing": 0, "unique": len(PATIENTS),
                                 "level_hashes": [], "known_levels": []},
                     "group": {"type": "character", "missing": 0, "unique": 2,
                               "level_hashes": [sha("Tumor"), sha("Non_Tumor")],
                               "known_levels": [{"Tumor": len(PATIENTS)},
                                                {"Non_Tumor": len(PATIENTS)}]}}},
        {"path": "data/signature/sets.rds", "sha256": digest(sets), "object_class": "list",
         "gene_sets": {"count": len(GENE_SETS), "sizes": [len(v) for v in GENE_SETS.values()],
                       "unique_members": len(GENES)},
         "set_name_sha256": [sha(name) for name in GENE_SETS],
         "keys": {"members": key(GENES)},
         "set_members": [[sha(gene) for gene in members] for members in GENE_SETS.values()]},
    ]
    audit = root / ".runtime/input_inspections/audit.json"
    audit.parent.mkdir(parents=True, exist_ok=True)
    audit.write_text(json.dumps({"profiles": profiles, "software": {}, "apis": {}}), encoding="utf-8")
    return root


def refresh_audit(root: Path) -> None:
    """Rewrite the trusted inspection after a fixture regenerates the real inputs."""
    audit = root / ".runtime/input_inspections/audit.json"
    local = json.loads(audit.read_text(encoding="utf-8"))
    for profile in local["profiles"]:
        path = root / profile["path"]
        profile["sha256"] = digest(path)
        profile["dimensions"] = (profile["dimensions"] if profile["path"].endswith(".csv")
                                 else [len(GENES), len(SAMPLES)])
    audit.write_text(json.dumps(local), encoding="utf-8")


def inspection(root: Path) -> dict:
    audit = root / ".runtime/input_inspections/audit.json"
    local = json.loads(audit.read_text(encoding="utf-8"))
    local["version"] = "input_probe_v2"
    local["audit_path"] = str(audit.relative_to(root))
    return local


def raw_plan(root: Path) -> dict:
    return {
        "goal": "GSVA 四方法通路评分", "analysis_unit": "patient",
        "method": "GSVA ssgsea/plage/zscore/gsva pathway scoring",
        "parameters": [], "limitations": ["Synthetic engineering fixture"],
        "inputs": ["data/raw/expr.rds", "data/metadata/samples.csv", "data/signature/sets.rds"],
        "input_bindings": {
            "matrices": [{"path": "data/raw/expr.rds", "sample_axis": "columns",
                          "metadata_path": "data/metadata/samples.csv",
                          "sample_key": "sample_id", "group_key": "group",
                          "group_map": {"Tumor": "Tumor", "Non_Tumor": "Non_Tumor"},
                          "pair_key": "patient", "preprocessing": "none"}],
            "gene_sets": ["data/signature/sets.rds"]},
        "steps": [{"id": "score_pathways", "language": "r",
                   "purpose": "Compute GSVA multi-method pathway scores and group tests",
                   "outputs": list(OUTPUTS.values())}],
        "research": {"enabled": True, "context": {
            "question": "Which pathways differ between tumour and adjacent tissue?",
            "disease": "liver cancer", "species": "Homo sapiens", "tissue": "liver",
            "analysis_unit": "patient", "cohort_id": "synthetic",
            "contrast": {"numerator": "Tumor", "denominator": "Non_Tumor"}, "limitations": []},
            "evidence_outputs": [{
                "path": "tables/gsva_significant_pathways.csv", "layer": "pathway_enrichment",
                "omics": "transcriptomics", "entity_type": "pathway", "namespace": "fixture",
                "columns": {"entity_id": "gene_set", "adjusted_p": "min_padj", "n": "n_units",
                            "selected": "selected", "status": "status"},
                "effect_scale": "unknown", "row_key": ["gene_set"], "measured_omics": [],
                "evidence_context": "derived_estimate",
                "selection": {"adjusted_p_max": 0.05}}]},
    }


# --------------------------------------------------------------------------- trigger

@pytest.mark.parametrize("text,expected", [
    ("用 GSVA 计算通路评分", ["gsva", "通路评分"]),
    ("compute ssGSEA pathway score", ["ssgsea", "pathway score"]),
    ("PLAGE 基因集评分", ["plage", "基因集评分"]),
    ("Calculate ZSCORE", ["zscore"]),
    ("统计差异基因数量", []),
    ("绘制通路热图", []),
])
def test_trigger_words_are_explicit_and_configurable(text, expected):
    assert scoring.trigger_hits(text, CFG["analysis"]["pathway_scoring"]) == expected


def test_trigger_defaults_when_configuration_is_absent():
    assert scoring.trigger_hits("使用 GSVA 计算通路评分", {}) == ["gsva", "通路评分"]
    assert scoring.trigger_hits("统计差异基因", {}) == []


@pytest.mark.parametrize("text,expected", [
    ("计算 GSVA 通路评分", ("ssgsea", "plage", "zscore", "gsva")),
    ("只用 plage 算法评分", ("plage",)),
    ("ssgsea 和 plage 都算", ("ssgsea", "plage", "zscore", "gsva")),
    ("GSVA 算法计算", ("gsva",)),
    ("PLAGE 与 ZSCORE 对比", ("ssgsea", "plage", "zscore", "gsva")),
])
def test_requested_methods_honour_one_explicit_choice_only(text, expected):
    assert scoring.requested_methods(text, CFG["analysis"]["pathway_scoring"]) == expected


def test_single_explicit_method_disables_the_consensus_rule():
    protocol = scoring.draft_protocol("只用 plage 算法评分", CFG)
    assert protocol["methods"] == ["plage"] and protocol["consensus_min_methods"] is None
    assert protocol["param_api"] == {"plage": "GSVA::plageParam"}


# ------------------------------------------------------------------------------ fdr

@pytest.mark.parametrize("text,value,source", [
    ("计算 GSVA 通路评分，FDR 0.01", 0.01, "user"),
    ("计算 GSVA 通路评分，FDR 5%", 0.05, "user"),
    ("计算 GSVA 通路评分", 0.05, "config:analysis.pathway_scoring.adjusted_p_max"),
])
def test_frozen_threshold_comes_from_the_request_or_configuration(text, value, source):
    protocol = scoring.draft_protocol(text, CFG)
    assert protocol["fdr"] == pytest.approx(value) and protocol["fdr_source"] == source


def test_figure_annotation_is_not_mistaken_for_a_threshold():
    # "-log10FDR" names an axis transform, not a significance level.
    protocol = scoring.draft_protocol("计算 GSVA 通路评分，颜色表示 -log10FDR", CFG)
    assert protocol["fdr"] == 0.05 and protocol["fdr_source"].startswith("config:")


def test_missing_threshold_fails_closed_without_configuration():
    cfg = {k: v for k, v in CFG.items() if k != "analysis"}
    with pytest.raises(ValueError, match="adjusted-p threshold"):
        scoring.frozen_protocol("计算 GSVA 通路评分", cfg, [], None, None)


def test_conflicting_thresholds_are_rejected():
    parameters = [{"name": "adjusted_p_max", "value": "0.01", "source": "user"}]
    with pytest.raises(ValueError, match="disagrees"):
        scoring.draft_protocol("计算 GSVA 通路评分，FDR 0.05", CFG, parameters)


def test_threshold_cannot_be_a_fixed_method_choice():
    parameters = [{"name": "fdr", "value": "0.05", "source": "fixed_method"}]
    with pytest.raises(ValueError, match="quoted from the request or read from configuration"):
        scoring.draft_protocol("计算 GSVA 通路评分", CFG, parameters)


def test_ambiguous_multiple_thresholds_are_rejected():
    with pytest.raises(ValueError, match="more than one adjusted-p threshold"):
        scoring.draft_protocol("计算 GSVA 通路评分，FDR 0.01，另外 padj 0.2", CFG)


# --------------------------------------------------------------------------- protocol

def test_protocol_freezes_test_contrast_and_outputs(tmp_path):
    root = build_project(tmp_path)
    raw = raw_plan(root)
    protocol = scoring.frozen_protocol(REQUEST, CFG, raw["parameters"],
                                       raw["input_bindings"], raw["research"])
    assert protocol["test"] == "paired_wilcoxon" and protocol["contrast_known"] is True
    assert protocol["contrast"] == {"numerator": "Tumor", "denominator": "Non_Tumor"}
    assert protocol["direction"] == "numerator_minus_denominator"
    assert protocol["multiple_testing"] == "BH within method family"
    assert set(protocol["outputs"]) == set(OUTPUTS)


def test_unpaired_binding_selects_the_rank_sum_test(tmp_path):
    root = build_project(tmp_path)
    raw = raw_plan(root)
    raw["input_bindings"]["matrices"][0]["pair_key"] = None
    protocol = scoring.frozen_protocol(REQUEST, CFG, [], raw["input_bindings"], raw["research"])
    assert protocol["test"] == "unpaired_mannwhitney"


def test_contrast_labels_must_be_the_mapped_groups(tmp_path):
    root = build_project(tmp_path)
    raw = raw_plan(root)
    raw["research"]["context"]["contrast"] = {"numerator": "tumour", "denominator": "adjacent"}
    with pytest.raises(ValueError, match="mapped group labels"):
        scoring.frozen_protocol(REQUEST, CFG, [], raw["input_bindings"], raw["research"])


def test_missing_contrast_is_recorded_as_unknown_direction(tmp_path):
    root = build_project(tmp_path)
    raw = raw_plan(root)
    raw["research"]["context"].pop("contrast")
    protocol = scoring.frozen_protocol(REQUEST, CFG, [], raw["input_bindings"], raw["research"])
    assert protocol["contrast_known"] is False and protocol["direction"] == "unknown"
    assert protocol["contrast_labels_source"] == "binding_order_unverified_direction"


def test_two_group_requirement_is_enforced(tmp_path):
    root = build_project(tmp_path)
    raw = raw_plan(root)
    raw["input_bindings"]["matrices"][0]["group_map"] = {"Tumor": "Tumor"}
    with pytest.raises(ValueError, match="exactly two mapped groups"):
        scoring.frozen_protocol(REQUEST, CFG, [], raw["input_bindings"], raw["research"])


def test_multiple_bound_matrices_are_rejected(tmp_path):
    root = build_project(tmp_path)
    raw = raw_plan(root)
    raw["input_bindings"]["matrices"] = raw["input_bindings"]["matrices"] * 2
    with pytest.raises(ValueError, match="exactly one bound expression matrix"):
        scoring.frozen_protocol(REQUEST, CFG, [], raw["input_bindings"], raw["research"])


def test_enforce_reports_every_missing_requirement_at_once(tmp_path):
    root = build_project(tmp_path)
    raw = raw_plan(root)
    protocol = scoring.frozen_protocol(REQUEST, CFG, [], raw["input_bindings"], raw["research"])
    raw["steps"][0]["outputs"] = ["tables/other.csv"]
    raw["steps"][0]["language"] = "python"
    raw["input_bindings"].pop("gene_sets")
    problems = " ".join(scoring.enforce(protocol, raw))
    for expected in ("gsva_scores.csv", "gsva_group_tests.csv", "at least one R step",
                     "gene-set resource"):
        assert expected in problems


def test_prompt_block_states_the_frozen_choices():
    protocol = scoring.draft_protocol(REQUEST, CFG)
    block = scoring.prompt_block(protocol)
    for expected in ("GSVA::ssgseaParam", "GSVA::plageParam", "GSVA::zscoreParam",
                     "GSVA::gsvaParam", "0.05", "zero variance",
                     "Never average, rank-combine"):
        assert expected in block


# ------------------------------------------------------------------------- plan gate

def test_scoring_plan_freezes_the_protocol_and_threshold_parameter(tmp_path):
    root = build_project(tmp_path)
    raw = raw_plan(root)
    checked = validate_plan(root, raw, REQUEST, CFG, inspection=inspection(root))
    protocol = checked["pathway_protocol"]
    assert protocol["methods"] == ["ssgsea", "plage", "zscore", "gsva"]
    assert protocol["fdr"] == pytest.approx(0.05)
    assert protocol["test"] == "paired_wilcoxon"
    # The frozen threshold is a traceable configuration parameter, not a model choice.
    assert any(p["name"] == "pathway_adjusted_p_max"
               and p["source"] == "config:analysis.pathway_scoring.adjusted_p_max"
               for p in checked["parameters"])


def test_protocol_change_changes_the_plan_hash(tmp_path):
    from multiomics_tui.task_sessions import canonical_hash
    root = build_project(tmp_path)
    raw = raw_plan(root)
    checked = validate_plan(root, raw, REQUEST, CFG, inspection=inspection(root))
    alternative = dict(checked["pathway_protocol"], fdr=0.01)
    assert canonical_hash({**checked, "pathway_protocol": alternative}) != canonical_hash(checked)


def test_plan_without_scoring_keeps_a_visible_limitation(tmp_path):
    root = build_project(tmp_path)
    raw = raw_plan(root)
    raw["method"] = "descriptive fixture projection"
    raw["steps"] = [{"id": "audit_inputs", "language": "python", "purpose": "Inspect inputs",
                     "outputs": ["tables/input_audit.csv"]}]
    raw["research"] = {"enabled": False}
    raw["input_bindings"] = {"inventory": True}
    checked = validate_plan(root, raw, "计算 GSVA 通路评分", CFG, inspection=inspection(root))
    assert "pathway_protocol" not in checked
    assert any("pathway_scoring_trigger_not_implemented" in item for item in checked["limitations"])


def test_plan_missing_protocol_outputs_is_rejected_with_all_problems(tmp_path):
    root = build_project(tmp_path)
    raw = raw_plan(root)
    # Keep the research-bound table so the protocol gate, not research, reports first.
    raw["steps"][0]["outputs"] = ["tables/gsva_scores.csv",
                                  "tables/gsva_significant_pathways.csv"]
    with pytest.raises(ValueError, match="frozen pathway-scoring protocol") as failure:
        validate_plan(root, raw, REQUEST, CFG, inspection=inspection(root))
    for expected in ("gsva_group_tests.csv", "gsva_gene_set_audit.csv", "gsva_protocol.json"):
        assert expected in str(failure.value)


def test_render_plan_discloses_the_protocol_in_both_languages(tmp_path):
    root = build_project(tmp_path)
    checked = validate_plan(root, raw_plan(root), REQUEST, CFG, inspection=inspection(root))
    english = render_plan("task", checked, "0" * 64)
    chinese = render_plan("task", checked, "0" * 64, language="zh", compact=True)
    for text in (english, chinese):
        assert "ssgsea, plage, zscore, gsva" in text
        assert "0.05" in text and "consensus" in text.lower() or "一致性" in text
    assert "Tumor - Non_Tumor" in english


# ------------------------------------------------------------------------ verifier

def _scores():
    """Deterministic per-method scores with a real tumour/adjacent shift.

    Values sit on a quarter-unit grid so a CSV round-trip reproduces them exactly;
    otherwise near-identical floats would silently break rank ties and the fixture,
    not the verifier, would be under test.
    """
    # Explicit per-pair effects: uniform ranks would give every gene set the same
    # p-value and hide a wrong multiple-testing scope. Mixed signs and ties here
    # produce distinct rank statistics within each method family.
    effects = {
        "SET_A": [1.0, 1.25, 1.5, 1.75],          # all positive, no ties
        "SET_B": [-0.75, -1.25, -0.5, -1.0],      # all negative
        "SET_C": [0.0, 0.0, 0.0, 0.0],            # identical differences
        "SET_D": [0.75, -0.25, 1.0, 0.25],        # one reversed pair
    }
    values = {}
    for method in scoring.METHODS:
        scale = 2.0 if method == "zscore" else 1.0
        shift = {"ssgsea": 0.0, "plage": 0.25, "zscore": 0.0, "gsva": -0.25}[method]
        values[method] = {}
        for gene_set in SCORED_SETS:
            row = {}
            for position, (sample, patient, group) in enumerate(metadata_rows()):
                patient_index = PATIENTS.index(patient)
                delta = effects[gene_set][patient_index] + shift * (1 if patient_index % 2 else -1)
                offset = 1.0 + patient_index * 0.25
                row[sample] = scale * (offset + (delta / 2.0 if group == "Tumor" else -delta / 2.0))
            values[method][gene_set] = row
    # A gene set with identical paired differences exercises the not-estimable branch.
    for method in scoring.METHODS:
        values[method]["SET_C"] = {sample: 0.5 for sample, _, _ in metadata_rows()}
    return values


def _statistics(values, protocol):
    """Reference recomputation used only to build fixtures for the tamper tests."""
    import numpy as np
    from scipy import stats

    rows = []
    for method in protocol["methods"]:
        for gene_set in sorted(values[method]):
            per_sample = values[method][gene_set]
            upper, lower = {}, {}
            for sample, patient, group in metadata_rows():
                if group == protocol["contrast"]["numerator"]:
                    upper[sample] = per_sample[sample]
                elif group == protocol["contrast"]["denominator"]:
                    lower[sample] = per_sample[sample]
            pairs = {}
            for sample, patient, group in metadata_rows():
                side = ("numerator" if group == protocol["contrast"]["numerator"]
                        else "denominator")
                pairs.setdefault(patient, {})[side] = per_sample[sample]
            delta = np.asarray([pair["numerator"] - pair["denominator"] for pair in pairs.values()],
                               dtype=float)
            n_units = len(delta)
            row = {"gene_set": gene_set, "method": method,
                   "family": "pathway_scoring_" + method, "test": protocol["test"],
                   "contrast_numerator": protocol["contrast"]["numerator"],
                   "contrast_denominator": protocol["contrast"]["denominator"],
                   "direction": protocol["direction"], "n_units": n_units,
                   "n_numerator": len(upper), "n_denominator": len(lower),
                   "effect_ci_method": "paired_t_mean_ci", "pvalue_method": "wilcoxon_signed_rank"}
            if np.all(delta == 0):
                row.update(status="not_estimable", reason="zero_difference", effect=None,
                           ci_low=None, ci_high=None, pvalue=None)
            else:
                ranks = stats.wilcoxon(delta, alternative="two-sided", correction=True,
                                       method="approx")
                interval = stats.t.interval(0.95, n_units - 1, loc=float(delta.mean()),
                                            scale=float(delta.std(ddof=1)) / math.sqrt(n_units))
                row.update(status="tested", reason=None, effect=float(delta.mean()),
                           ci_low=float(interval[0]), ci_high=float(interval[1]),
                           pvalue=float(ranks.pvalue))
            rows.append(row)
    return rows


def _write_tables(task_dir: Path, values, protocol, *, fdr=None):
    import numpy as np
    import pandas as pd
    from scipy.stats import false_discovery_control

    task_dir.joinpath("tables").mkdir(parents=True, exist_ok=True)
    task_dir.joinpath("reports").mkdir(parents=True, exist_ok=True)
    rows = _statistics(values, protocol)
    for method in protocol["methods"]:
        block = [row for row in rows if row["method"] == method]
        tested = [row for row in block if row["status"] == "tested"]
        adjusted = false_discovery_control(np.asarray([row["pvalue"] for row in tested]), method="bh")
        for row, value in zip(tested, adjusted):
            row["padj"] = float(value)
        for row in block:
            row.setdefault("padj", None)
            row["selected"] = bool(row["status"] == "tested"
                                   and row["padj"] <= (fdr or protocol["fdr"]))
    long_rows = [{"gene_set": gene_set, "sample_id": sample, "score": score, "method": method}
                 for method in protocol["methods"] for gene_set, row in values[method].items()
                 for sample, score in row.items()]
    pd.DataFrame(long_rows)[list(scoring.SCORES_COLUMNS)].to_csv(
        task_dir / protocol["outputs"]["scores"], index=False)
    pd.DataFrame(rows)[list(scoring.TESTS_COLUMNS)].to_csv(
        task_dir / protocol["outputs"]["tests"], index=False)
    summary = []
    for gene_set in sorted(values[protocol["methods"][0]]):
        block = [row for row in rows if row["gene_set"] == gene_set]
        tested = [row for row in block if row["status"] == "tested"]
        selected = [row for row in block if row["selected"]]
        effects = np.asarray([row["effect"] for row in tested], dtype=float)
        consistent = (None if (len(effects) < 2 or not protocol["contrast_known"])
                      else bool((effects > 0).all() or (effects < 0).all()))
        minimum = float(min(row["padj"] for row in tested)) if tested else None
        chosen = bool(tested) and minimum <= protocol["fdr"]
        summary.append({
            "gene_set": gene_set, "methods_tested": len(tested),
            "methods_significant": len(selected),
            "significant_methods": ";".join(sorted(row["method"] for row in selected)),
            "min_padj": minimum,
            "max_abs_effect": float(np.abs(effects).max()) if tested else None,
            "direction_consistent": consistent, "n_units": block[0]["n_units"],
            "selected": chosen,
            "consensus": bool(protocol["consensus_min_methods"] and chosen
                              and len(selected) >= protocol["consensus_min_methods"]
                              and (consistent is not False)),
            "status": "tested" if tested else "not_estimable",
            "reason": None if tested else ";".join(sorted({row["reason"] for row in block}))})
    pd.DataFrame(summary)[list(scoring.SIGNIFICANT_COLUMNS)].to_csv(
        task_dir / protocol["outputs"]["significant"], index=False)
    audit_rows = []
    constant = {"GENE1", "GENE2"}
    for gene_set, members in GENE_SETS.items():
        overlap_all = len(members)
        overlap_variable = len([gene for gene in members if gene not in constant])
        if overlap_all == 0:
            status, reason = "zero_overlap", "no member measured in the expression matrix"
        elif overlap_variable == 0:
            status, reason = "no_variable_features", "all measured members are constant"
        elif overlap_variable < protocol["min_size"]:
            status, reason = "size_below_min", "fewer members than the frozen minSize"
        elif overlap_variable > protocol["max_size"]:
            status, reason = "size_above_max", "more members than the frozen maxSize"
        else:
            status, reason = "used", None
        audit_rows.append({"gene_set": gene_set, "size_in_resource": len(members),
                           "size_in_matrix": overlap_variable, "status": status, "reason": reason})
    pd.DataFrame(audit_rows)[list(scoring.AUDIT_COLUMNS)].to_csv(
        task_dir / protocol["outputs"]["gene_sets"], index=False)
    report = {"methods": protocol["methods"], "fdr": protocol["fdr"],
              "min_size": protocol["min_size"], "max_size": protocol["max_size"],
              "test": protocol["test"], "contrast": protocol["contrast"],
              "contrast_known": protocol["contrast_known"],
              "contrast_labels_source": protocol["contrast_labels_source"],
              "seed": protocol["seed"],
              "consensus_min_methods": protocol["consensus_min_methods"],
              "gsva_version": "2.4.9", "r_version": "R version fixture",
              "session_info": "fixture session", "dropped_features": 0,
              "dropped_gene_sets": [], "warnings": []}
    (task_dir / protocol["outputs"]["protocol"]).write_text(json.dumps(report), encoding="utf-8")
    return task_dir


def _verified(tmp_path):
    root = build_project(tmp_path / "project")
    checked = validate_plan(root, raw_plan(root), REQUEST, CFG, inspection=inspection(root))
    task_dir = _write_tables(root / "analysis/task", _scores(), checked["pathway_protocol"])
    return root, checked, task_dir


def test_verifier_accepts_a_self_consistent_contract(tmp_path):
    root, checked, task_dir = _verified(tmp_path)
    result = scoring.verify_outputs(root, task_dir, checked)
    assert result["status"] == "passed"
    assert result["methods"] == ["ssgsea", "plage", "zscore", "gsva"]
    assert result["not_estimable_rows"] >= 1 and result["n_units"] == len(PATIENTS)


@pytest.mark.parametrize("mutation,message", [
    ("padj", "padj"),
    ("selected", "selected"),
    ("consensus", "consensus"),
    ("pvalue", "pvalue"),
    ("status", "status"),
    ("methods_tested", "methods_tested"),
])
def test_verifier_rejects_edited_statistics(tmp_path, mutation, message):
    import pandas as pd
    root, checked, task_dir = _verified(tmp_path)
    tests_path = task_dir / checked["pathway_protocol"]["outputs"]["tests"]
    significant_path = task_dir / checked["pathway_protocol"]["outputs"]["significant"]
    if mutation in {"consensus", "methods_tested"}:
        frame = pd.read_csv(significant_path)
        if mutation == "consensus":
            frame.loc[frame.index[0], "consensus"] = not bool(frame.loc[frame.index[0], "consensus"])
        else:
            frame.loc[frame.index[0], "methods_tested"] = int(frame.loc[frame.index[0],
                                                                      "methods_tested"]) + 1
        frame.to_csv(significant_path, index=False)
    else:
        frame = pd.read_csv(tests_path)
        tested = frame.index[frame.status == "tested"]
        if mutation == "padj":
            frame.loc[tested, "padj"] = frame.loc[tested, "padj"].to_numpy(dtype=float) * 1.5
        elif mutation == "selected":
            frame.loc[tested[0], "selected"] = not bool(frame.loc[tested[0], "selected"])
        elif mutation == "pvalue":
            frame.loc[tested[0], "pvalue"] = float(frame.loc[tested[0], "pvalue"]) / 10.0
        elif mutation == "status":
            frame.loc[tested[0], "status"] = "not_estimable"
        frame.to_csv(tests_path, index=False)
    with pytest.raises(ValueError, match=message):
        scoring.verify_outputs(root, task_dir, checked)


def test_multiple_testing_scope_is_one_family_per_method():
    import numpy as np
    import pandas as pd
    from scipy.stats import false_discovery_control
    frame = pd.DataFrame({
        "family": ["pathway_scoring_ssgsea"] * 3 + ["pathway_scoring_plage"] * 5,
        "status": ["tested"] * 8,
        "pvalue": [0.001, 0.02, 0.04, 0.5, 0.01, 0.03, 0.2, 0.6]})
    adjusted = scoring._bh(frame)
    per_family = np.concatenate([
        false_discovery_control(np.asarray([0.001, 0.02, 0.04]), method="bh"),
        false_discovery_control(np.asarray([0.5, 0.01, 0.03, 0.2, 0.6]), method="bh")])
    global_bh = false_discovery_control(frame.pvalue.to_numpy(dtype=float), method="bh")
    assert np.allclose(adjusted, per_family)
    assert not np.allclose(per_family, global_bh)


def test_verifier_rejects_a_dropped_pathway(tmp_path):
    import pandas as pd
    root, checked, task_dir = _verified(tmp_path)
    scores_path = task_dir / checked["pathway_protocol"]["outputs"]["scores"]
    frame = pd.read_csv(scores_path)
    frame[frame.method != "plage"].to_csv(scores_path, index=False)
    with pytest.raises(ValueError, match="methods differ"):
        scoring.verify_outputs(root, task_dir, checked)




def test_verifier_rejects_a_jointly_dropped_pathway_set(tmp_path):
    import pandas as pd
    root, checked, task_dir = _verified(tmp_path)
    protocol = checked["pathway_protocol"]
    values = _scores()
    for method in values:
        for gene_set in ("SET_B", "SET_C", "SET_D"):
            del values[method][gene_set]
    _write_tables(task_dir, values, protocol)
    audit_path = task_dir / protocol["outputs"]["gene_sets"]
    audit = pd.read_csv(audit_path)
    audit = audit.loc[~audit.gene_set.isin(["SET_B", "SET_C", "SET_D"])]
    audit.to_csv(audit_path, index=False)
    with pytest.raises(ValueError, match="frozen gene-set universe|resource universe"):
        scoring.verify_outputs(root, task_dir, checked)


def test_verifier_rejects_a_changed_audit_status_or_size(tmp_path):
    import pandas as pd
    root, checked, task_dir = _verified(tmp_path)
    audit_path = task_dir / checked["pathway_protocol"]["outputs"]["gene_sets"]
    original = pd.read_csv(audit_path)
    used = original.index[original.status == "used"][0]
    for column, value, message in (
            ("status", "size_below_min", "status differs"),
            ("size_in_matrix", int(original.loc[used, "size_in_matrix"]) - 1, "size_in_matrix differs"),
            ("size_in_resource", int(original.loc[used, "size_in_resource"]) + 1, "size_in_resource differs")):
        frame = original.copy()
        frame.loc[used, column] = value
        frame.to_csv(audit_path, index=False)
        with pytest.raises(ValueError, match=message):
            scoring.verify_outputs(root, task_dir, checked)


def test_verifier_rejects_a_changed_audit_reason_or_missing_row(tmp_path):
    import pandas as pd
    root, checked, task_dir = _verified(tmp_path)
    audit_path = task_dir / checked["pathway_protocol"]["outputs"]["gene_sets"]
    original = pd.read_csv(audit_path)
    used = original.index[original.status == "used"][0]
    frame = original.copy()
    frame.loc[used, "reason"] = "changed justification"
    frame.to_csv(audit_path, index=False)
    with pytest.raises(ValueError, match="Used gene set carries a filter reason"):
        scoring.verify_outputs(root, task_dir, checked)
    original.iloc[:-1].to_csv(audit_path, index=False)
    with pytest.raises(ValueError, match="resource universe"):
        scoring.verify_outputs(root, task_dir, checked)


def test_verifier_rejects_a_changed_frozen_universe(tmp_path):
    root, checked, task_dir = _verified(tmp_path)
    tampered = dict(checked)
    tampered["pathway_protocol"] = dict(checked["pathway_protocol"])
    tampered["pathway_protocol"]["gene_set_universe"] = list(
        checked["pathway_protocol"]["gene_set_universe"])
    tampered["pathway_protocol"]["gene_set_universe"][0] = dict(
        tampered["pathway_protocol"]["gene_set_universe"][0], size_in_matrix=999)
    with pytest.raises(ValueError, match="Frozen gene-set universe differs"):
        scoring.verify_outputs(root, task_dir, tampered)


def test_plan_freezes_the_gene_set_universe(tmp_path):
    root = build_project(tmp_path)
    checked = validate_plan(root, raw_plan(root), REQUEST, CFG, inspection=inspection(root))
    universe = checked["pathway_protocol"]["gene_set_universe"]
    assert {record["status"] for record in universe} >= {"used", "no_variable_features"}
    assert all("gene_set_sha256" in record for record in universe)
    assert checked["pathway_protocol"]["gene_set_resources"][0]["path"] == "data/signature/sets.rds"


def test_all_not_estimable_is_retained_without_p_range_error(tmp_path):
    root, checked, task_dir = _verified(tmp_path)
    values = _scores()
    for method in values:
        for gene_set in values[method]:
            for sample in values[method][gene_set]:
                values[method][gene_set][sample] = 0.5
    _write_tables(task_dir, values, checked["pathway_protocol"])
    result = scoring.verify_outputs(root, task_dir, checked)
    assert result["status"] == "passed"
    assert result["statistical_status"] == "not_estimable"
    assert result["tested_rows"] == 0
    assert result["not_estimable_rows"] == 16
    assert result["selected_rows"] == 0


def test_verifier_rejects_a_silently_dropped_sample(tmp_path):
    import pandas as pd
    root, checked, task_dir = _verified(tmp_path)
    scores_path = task_dir / checked["pathway_protocol"]["outputs"]["scores"]
    frame = pd.read_csv(scores_path)
    frame[frame.sample_id != SAMPLES[0]].to_csv(scores_path, index=False)
    with pytest.raises(ValueError, match="dropped samples|do not match"):
        scoring.verify_outputs(root, task_dir, checked)


def test_verifier_rejects_a_missing_report_field(tmp_path):
    root, checked, task_dir = _verified(tmp_path)
    path = task_dir / checked["pathway_protocol"]["outputs"]["protocol"]
    report = json.loads(path.read_text(encoding="utf-8"))
    report.pop("session_info")
    path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ValueError, match="session_info"):
        scoring.verify_outputs(root, task_dir, checked)


def test_verifier_rejects_a_threshold_that_does_not_match_the_plan(tmp_path):
    import pandas as pd
    root, checked, task_dir = _verified(tmp_path)
    path = task_dir / checked["pathway_protocol"]["outputs"]["protocol"]
    report = json.loads(path.read_text(encoding="utf-8"))
    report["fdr"] = 0.2
    path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ValueError, match="fdr"):
        scoring.verify_outputs(root, task_dir, checked)


def test_verifier_rejects_an_unknown_gene_set_status(tmp_path):
    import pandas as pd
    root, checked, task_dir = _verified(tmp_path)
    path = task_dir / checked["pathway_protocol"]["outputs"]["gene_sets"]
    frame = pd.read_csv(path)
    frame.loc[frame.index[0], "status"] = "probably_fine"
    frame.to_csv(path, index=False)
    with pytest.raises(ValueError, match="vocabulary"):
        scoring.verify_outputs(root, task_dir, checked)


def test_verifier_rejects_a_plan_without_protocol(tmp_path):
    root, checked, task_dir = _verified(tmp_path)
    with pytest.raises(ValueError, match="no frozen pathway-scoring protocol"):
        scoring.verify_outputs(root, task_dir, {k: v for k, v in checked.items()
                                                if k != "pathway_protocol"})


def test_execution_blocks_when_the_frozen_protocol_no_longer_matches(tmp_path):
    """A configuration change after approval must not silently change the protocol."""
    from multiomics_tui import task_runner
    from multiomics_tui.task_sessions import TaskStore
    root = build_project(tmp_path)
    store = TaskStore(root)
    state = store.create(REQUEST_WITH_THRESHOLD)
    # The user text is what the threshold quotation is validated against.
    store.event(state["task_id"], "user", REQUEST_WITH_THRESHOLD)
    checked = validate_plan(root, raw_plan(root), REQUEST_WITH_THRESHOLD, CFG,
                            inspection=inspection(root))
    state = store.save_plan(state["task_id"], checked)
    state = store.approve(state["task_id"], state["plan_hash"])
    (root / "config/study_config.yaml").write_text(
        "schema_version: '1.0'\nanalysis:\n  pathway_scoring:\n    methods: [plage]\n",
        encoding="utf-8")
    with pytest.raises(ValueError, match="no longer matches the approved plan"):
        task_runner.run(root, state["task_id"], client=None)


# ------------------------------------------------------------------------- real R

def host_r() -> tuple[str, list[str]]:
    rscript = shutil.which("Rscript")
    if not rscript:
        pytest.skip("Rscript is unavailable")
    project_library = Path(__file__).resolve().parents[1] / ".runtime/r-library"
    libraries = [str(project_library)] if project_library.is_dir() else []
    result = subprocess.run([rscript, "--vanilla", "-e",
                             'cat(as.character(utils::packageVersion("GSVA")))'],
                            capture_output=True, text=True,
                            env={"R_LIBS_USER": ":".join(libraries), "PATH": "/usr/bin:/bin:/usr/local/bin"})
    if result.returncode:
        pytest.skip("GSVA is not installed: " + result.stderr.strip()[-200:])
    return rscript, libraries


SCORING_R = r"""
a <- commandArgs(TRUE)
expr_path <- a[1]; sets_path <- a[2]; meta_path <- a[3]; out <- a[4]
fdr <- as.numeric(a[5]); seed <- as.integer(a[6])
dir.create(file.path(out, 'tables'), recursive = TRUE, showWarnings = FALSE)
dir.create(file.path(out, 'reports'), recursive = TRUE, showWarnings = FALSE)
set.seed(seed)
expr <- readRDS(expr_path); sets <- readRDS(sets_path)
meta <- utils::read.csv(meta_path, stringsAsFactors = FALSE)
expr_all <- expr
keep <- apply(expr, 1L, stats::var) > 0
dropped_features <- sum(!keep)
expr <- expr[keep, , drop = FALSE]
methods <- c('ssgsea', 'plage', 'zscore', 'gsva')
params <- list(
  ssgsea = GSVA::ssgseaParam(expr, sets, minSize = 5, maxSize = 500, normalize = TRUE),
  plage = GSVA::plageParam(expr, sets, minSize = 5, maxSize = 500),
  zscore = GSVA::zscoreParam(expr, sets, minSize = 5, maxSize = 500),
  gsva = GSVA::gsvaParam(expr, sets, minSize = 5, maxSize = 500, kcdf = 'Gaussian'))
upper <- meta[meta$group == 'Tumor', ]; lower <- meta[meta$group == 'Non_Tumor', ]
upper <- upper[order(upper$patient), ]; lower <- lower[order(lower$patient), ]
stopifnot(identical(upper$patient, lower$patient))
long_rows <- list(); test_rows <- list()
for (mt in methods) {
  s <- GSVA::gsva(params[[mt]], verbose = FALSE, BPPARAM = BiocParallel::SerialParam())
  long_rows[[mt]] <- data.frame(
    gene_set = rep(rownames(s), times = ncol(s)),
    sample_id = rep(colnames(s), each = nrow(s)),
    score = as.vector(s), method = mt, stringsAsFactors = FALSE)
  for (gs in rownames(s)) {
    d <- s[gs, upper$sample_id] - s[gs, lower$sample_id]
    if (all(d == 0)) {
      test_rows[[length(test_rows) + 1L]] <- data.frame(
        gene_set = gs, method = mt, family = paste0('pathway_scoring_', mt),
        test = 'paired_wilcoxon', contrast_numerator = 'Tumor',
        contrast_denominator = 'Non_Tumor', direction = 'numerator_minus_denominator',
        n_units = length(d), n_numerator = nrow(upper), n_denominator = nrow(lower),
        effect = NA_real_, ci_low = NA_real_, ci_high = NA_real_, pvalue = NA_real_,
        padj = NA_real_, selected = FALSE, status = 'not_estimable',
        reason = 'zero_difference', effect_ci_method = 'paired_t_mean_ci',
        pvalue_method = 'wilcoxon_signed_rank', stringsAsFactors = FALSE)
    } else {
      # One-sample signed-rank on the paired differences is the paired test.
      wt <- stats::wilcox.test(d, mu = 0, exact = FALSE, correct = TRUE)
      tt <- stats::t.test(d)
      test_rows[[length(test_rows) + 1L]] <- data.frame(
        gene_set = gs, method = mt, family = paste0('pathway_scoring_', mt),
        test = 'paired_wilcoxon', contrast_numerator = 'Tumor',
        contrast_denominator = 'Non_Tumor', direction = 'numerator_minus_denominator',
        n_units = length(d), n_numerator = nrow(upper), n_denominator = nrow(lower),
        effect = unname(tt$estimate), ci_low = tt$conf.int[1], ci_high = tt$conf.int[2],
        pvalue = wt$p.value, padj = NA_real_, selected = FALSE, status = 'tested',
        reason = NA_character_, effect_ci_method = 'paired_t_mean_ci',
        pvalue_method = 'wilcoxon_signed_rank', stringsAsFactors = FALSE)
    }
  }
}
tests <- do.call(rbind, test_rows)
for (mt in methods) {
  block <- tests$method == mt & tests$status == 'tested'
  tests$padj[block] <- stats::p.adjust(tests$pvalue[block], method = 'BH')
  tests$selected[tests$method == mt] <- !is.na(tests$padj[tests$method == mt]) &
    tests$padj[tests$method == mt] <= fdr
}
utils::write.csv(do.call(rbind, long_rows), file.path(out, 'tables/gsva_scores.csv'), row.names = FALSE)
utils::write.csv(tests, file.path(out, 'tables/gsva_group_tests.csv'), row.names = FALSE)
summary_rows <- list()
for (gs in sort(unique(tests$gene_set))) {
  block <- tests[tests$gene_set == gs, ]
  tested <- block[block$status == 'tested', ]
  chosen <- block[block$selected, ]
  effects <- tested$effect
  consistent <- if (nrow(tested) < 2) NA else (all(effects > 0) || all(effects < 0))
  minimum <- if (nrow(tested)) min(tested$padj) else NA_real_
  selected <- is.finite(minimum) && minimum <= fdr
  summary_rows[[length(summary_rows) + 1L]] <- data.frame(
    gene_set = gs, methods_tested = nrow(tested), methods_significant = nrow(chosen),
    significant_methods = paste(sort(chosen$method), collapse = ';'),
    min_padj = minimum,
    max_abs_effect = if (nrow(tested)) max(abs(effects)) else NA_real_,
    direction_consistent = consistent, n_units = block$n_units[1], selected = selected,
    consensus = selected && nrow(chosen) >= 2 && isTRUE(consistent),
    status = if (nrow(tested)) 'tested' else 'not_estimable',
    reason = if (nrow(tested)) NA_character_ else paste(sort(unique(block$reason)), collapse = ';'),
    stringsAsFactors = FALSE)
}
utils::write.csv(do.call(rbind, summary_rows),
                 file.path(out, 'tables/gsva_significant_pathways.csv'), row.names = FALSE)
# Audit every resource set; sets outside the frozen size limits are reported, not scored.
overlap_all <- vapply(names(sets), function(n) sum(sets[[n]] %in% rownames(expr_all)), integer(1))
overlap_variable <- vapply(names(sets), function(n) sum(sets[[n]] %in% rownames(expr)), integer(1))
status <- ifelse(overlap_all == 0, 'zero_overlap',
                 ifelse(overlap_variable == 0, 'no_variable_features',
                        ifelse(overlap_variable < 5, 'size_below_min',
                               ifelse(overlap_variable > 500, 'size_above_max', 'used'))))
reason <- ifelse(status == 'used', NA_character_,
                 ifelse(status == 'zero_overlap', 'no member measured in the expression matrix',
                        ifelse(status == 'no_variable_features', 'all measured members are constant',
                               'outside the frozen gene-set size limits')))
utils::write.csv(data.frame(gene_set = names(sets), size_in_resource = lengths(sets),
                            size_in_matrix = overlap_variable, status = status, reason = reason,
                            stringsAsFactors = FALSE),
                 file.path(out, 'tables/gsva_gene_set_audit.csv'), row.names = FALSE)
jsonlite::write_json(list(methods = methods, fdr = fdr, min_size = 5, max_size = 500,
                          test = 'paired_wilcoxon',
                          contrast = list(numerator = 'Tumor', denominator = 'Non_Tumor'),
                          contrast_known = TRUE,
                          contrast_labels_source = 'approved_research_contrast',
                          seed = seed, consensus_min_methods = 2,
                          gsva_version = as.character(utils::packageVersion('GSVA')),
                          r_version = R.version.string,
                          session_info = paste(utils::capture.output(utils::sessionInfo()),
                                               collapse = '\n'),
                          dropped_features = dropped_features,
                          dropped_gene_sets = list(), warnings = list()),
                     file.path(out, 'reports/gsva_protocol.json'), auto_unbox = TRUE, pretty = TRUE)
"""


def _write_r_fixture(root: Path) -> None:
    """Real numeric matrix, named gene sets and paired metadata for the R run."""
    rscript, libraries = host_r()
    script = root / "fixture.R"
    script.write_text(
        "a <- commandArgs(TRUE)\n"
        "set.seed(11)\n"
        f"genes <- paste0('GENE', 1:{len(GENES)})\n"
        f"samples <- paste0('S', 1:{len(SAMPLES)})\n"
        "m <- matrix(stats::rnorm(length(genes) * length(samples), 6, 1.2), length(genes),\n"
        "            length(samples), dimnames = list(genes, samples))\n"
        # Two features never vary across samples: plage/zscore/gsva fail without filtering.
        "m[1, ] <- 5\n"
        "m[2, ] <- 5\n"
        "saveRDS(m, a[1])\n"
        "sets <- list(SET_A = genes[1:12], SET_B = genes[13:24], SET_C = genes[25:32],\n"
        "             SET_D = genes[33:40], SET_TINY = genes[1:2])\n"
        "saveRDS(sets, a[2])\n",
        encoding="utf-8")
    subprocess.run([rscript, "--vanilla", str(script), str(root / "data/raw/expr.rds"),
                    str(root / "data/signature/sets.rds")], check=True, capture_output=True,
                   env={"R_LIBS_USER": ":".join(libraries), "PATH": "/usr/bin:/bin:/usr/local/bin"})


def test_real_gsva_tables_pass_the_verifier(tmp_path):
    rscript, libraries = host_r()
    root = build_project(tmp_path)
    _write_r_fixture(root)
    refresh_audit(root)
    checked = validate_plan(root, raw_plan(root), REQUEST, CFG, inspection=inspection(root))
    task_dir = root / "analysis/task"
    runner = root / "score.R"
    runner.write_text(SCORING_R, encoding="utf-8")
    result = subprocess.run(
        [rscript, "--vanilla", str(runner), str(root / "data/raw/expr.rds"),
         str(root / "data/signature/sets.rds"), str(root / "data/metadata/samples.csv"),
         str(task_dir), str(checked["pathway_protocol"]["fdr"]),
         str(checked["pathway_protocol"]["seed"])],
        capture_output=True, text=True,
        env={"R_LIBS_USER": ":".join(libraries), "PATH": "/usr/bin:/bin:/usr/local/bin"})
    assert result.returncode == 0, result.stderr[-2000:]
    verified = scoring.verify_outputs(root, task_dir, checked)
    assert verified["status"] == "passed" and verified["methods"] == list(scoring.METHODS)
    # Zero-variance features are dropped and recorded, not silently ignored.
    report = json.loads((task_dir / "reports/gsva_protocol.json").read_text(encoding="utf-8"))
    assert report["dropped_features"] == 2


def test_real_gsva_reference_script_reproduces_the_scores(tmp_path):
    rscript, libraries = host_r()
    root = build_project(tmp_path)
    _write_r_fixture(root)
    refresh_audit(root)
    checked = validate_plan(root, raw_plan(root), REQUEST, CFG, inspection=inspection(root))
    task_dir = root / "analysis/task"
    runner = root / "score.R"
    runner.write_text(SCORING_R, encoding="utf-8")
    subprocess.run(
        [rscript, "--vanilla", str(runner), str(root / "data/raw/expr.rds"),
         str(root / "data/signature/sets.rds"), str(root / "data/metadata/samples.csv"),
         str(task_dir), "0.05", "2024"],
        check=True, capture_output=True,
        env={"R_LIBS_USER": ":".join(libraries), "PATH": "/usr/bin:/bin:/usr/local/bin"})
    protocol = checked["pathway_protocol"]
    protocol_path = root / "protocol.json"
    protocol_path.write_text(json.dumps({"methods": protocol["methods"], "min_size": 5,
                                         "max_size": 500, "seed": 2024}), encoding="utf-8")
    reference = Path(__file__).resolve().parents[1] / "scripts/32_gsva_multimethod_reference.R"
    output = root / "reference.json"
    result = subprocess.run(
        [rscript, "--vanilla", str(reference), str(root / "data/raw/expr.rds"),
         str(root / "data/signature/sets.rds"), str(protocol_path),
         str(task_dir / "tables/gsva_scores.csv"), str(output)],
        capture_output=True, text=True,
        env={"R_LIBS_USER": ":".join(libraries), "PATH": "/usr/bin:/bin:/usr/local/bin"})
    assert result.returncode == 0, result.stderr[-2000:]
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "passed" and report["dropped_features"] == 2
    assert report["scored_methods"] == list(scoring.METHODS)
    assert max(report["max_abs_error"].values()) <= 1e-8


def test_real_gsva_reference_detects_a_scrambled_score_table(tmp_path):
    import pandas as pd
    rscript, libraries = host_r()
    root = build_project(tmp_path)
    _write_r_fixture(root)
    refresh_audit(root)
    task_dir = root / "analysis/task"
    runner = root / "score.R"
    runner.write_text(SCORING_R, encoding="utf-8")
    subprocess.run(
        [rscript, "--vanilla", str(runner), str(root / "data/raw/expr.rds"),
         str(root / "data/signature/sets.rds"), str(root / "data/metadata/samples.csv"),
         str(task_dir), "0.05", "2024"],
        check=True, capture_output=True,
        env={"R_LIBS_USER": ":".join(libraries), "PATH": "/usr/bin:/bin:/usr/local/bin"})
    long_path = task_dir / "tables/gsva_scores.csv"
    frame = pd.read_csv(long_path)
    frame.loc[0, "score"] = float(frame.loc[0, "score"]) + 0.5
    frame.to_csv(long_path, index=False)
    protocol_path = root / "protocol.json"
    protocol_path.write_text(json.dumps({"methods": list(scoring.METHODS), "min_size": 5,
                                         "max_size": 500, "seed": 2024}), encoding="utf-8")
    reference = Path(__file__).resolve().parents[1] / "scripts/32_gsva_multimethod_reference.R"
    result = subprocess.run(
        [rscript, "--vanilla", str(reference), str(root / "data/raw/expr.rds"),
         str(root / "data/signature/sets.rds"), str(protocol_path), str(long_path),
         str(root / "reference.json")],
        capture_output=True, text=True,
        env={"R_LIBS_USER": ":".join(libraries), "PATH": "/usr/bin:/bin:/usr/local/bin"})
    assert result.returncode != 0 and "score mismatch" in result.stderr


# ------------------------------------------------------------------------- config

def test_example_configuration_ships_the_protocol_defaults():
    import yaml
    path = Path(__file__).resolve().parents[1] / "config/study_config.example.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    section = config["analysis"]["pathway_scoring"]
    assert section["adjusted_p_max"] == 0.05
    assert section["methods"] == ["ssgsea", "plage", "zscore", "gsva"]
    assert section["consensus_min_methods"] == 2
    assert "gsva" in section["trigger"] and "通路评分" in section["trigger"]
    protocol = scoring.draft_protocol(REQUEST, config)
    assert protocol["fdr"] == pytest.approx(0.05)

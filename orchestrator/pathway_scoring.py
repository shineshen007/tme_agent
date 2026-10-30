"""Deterministic GSVA multi-method pathway-scoring protocol and its verifier.

The protocol is produced here, never by the model: the trigger words, the method
set, the gene-set size limits, the frozen adjusted-p threshold, the group test and
the cross-method consensus rule are resolved locally from configuration plus
explicit user text. A plan that scores pathways therefore cannot invent its own
thresholds, silently drop a method, or rename the comparison direction.

``verify_outputs`` recomputes every rank test, every BH adjustment, every selection
flag and every cross-method count from the sample-level score table with mature
SciPy APIs (``wilcoxon``, ``mannwhitneyu``, ``false_discovery_control``, ``t.interval``).
A table therefore cannot be made "significant" by editing p-values or by dropping a
pathway. What this verifier cannot prove is that the score matrix was actually
produced by the named GSVA method; ``scripts/32_gsva_multimethod_reference.R`` checks
that separately on synthetic data. Engineering checks are not scientific validation.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path

METHODS = ("ssgsea", "plage", "zscore", "gsva")
PARAM_API = {"ssgsea": "GSVA::ssgseaParam", "plage": "GSVA::plageParam",
             "zscore": "GSVA::zscoreParam", "gsva": "GSVA::gsvaParam"}
DEFAULT_METHODS = METHODS
DEFAULT_TRIGGERS = ("gsva", "ssgsea", "plage", "zscore", "通路评分", "通路得分",
                    "基因集评分", "pathway score", "gene set score")
FDR_PARAMETER_NAMES = {"pathway_adjusted_p_max", "adjusted_p_max", "fdr"}
OUTPUTS = {"scores": "tables/gsva_scores.csv", "tests": "tables/gsva_group_tests.csv",
           "significant": "tables/gsva_significant_pathways.csv",
           "gene_sets": "tables/gsva_gene_set_audit.csv",
           "protocol": "reports/gsva_protocol.json"}
SCORES_COLUMNS = ("gene_set", "sample_id", "score", "method")
TESTS_COLUMNS = ("gene_set", "method", "family", "test", "contrast_numerator",
                 "contrast_denominator", "direction", "n_units", "n_numerator",
                 "n_denominator", "effect", "ci_low", "ci_high", "pvalue", "padj",
                 "selected", "status", "reason", "effect_ci_method", "pvalue_method")
SIGNIFICANT_COLUMNS = ("gene_set", "methods_tested", "methods_significant",
                       "significant_methods", "min_padj", "max_abs_effect",
                       "direction_consistent", "n_units", "selected", "consensus",
                       "status", "reason")
AUDIT_COLUMNS = ("gene_set", "size_in_resource", "size_in_matrix", "status", "reason")
TESTED = "tested"
NOT_ESTIMABLE = "not_estimable"
REASONS = {"zero_difference", "fewer_than_three_units", "nonfinite_estimate", "wilcox_failed"}
AUDIT_STATUSES = {"used", "size_below_min", "size_above_max", "zero_overlap",
                  "no_variable_features"}
MIN_UNITS = 3
TOLERANCE = 1e-8
SCORE_MEANING = ("relative gene-set enrichment score; not a cell fraction, metabolic flux, "
                 "enzyme activity, pathway activation proof or causal evidence")

# A named method token must stand alone. "GSVA" alone names the package, so it only
# counts as a requested method when the text calls it a method ("gsva算法/方法/method").
_METHOD_TOKEN = re.compile(r"(?<![A-Za-z0-9_])(ssgsea|plage|zscore|gsva)(?![A-Za-z0-9_])", re.I)
_GSVA_METHOD_QUALIFIER = re.compile(r"(?i)gsva\s*(?:算法|方法|法|method|algorithm)")
_THRESHOLD_PATTERNS = (
    re.compile(r"(?i)(?:fdr|padj|adjusted[\s_-]*p|校正后?\s*p|显著性水平|alpha)"
               r"[^。；;\n]{0,14}?(\d+(?:\.\d+)?(?:[eE]-?\d+)?)\s*%?"),
    re.compile(r"(?i)(\d+(?:\.\d+)?(?:[eE]-?\d+)?)\s*%?\s*(?:的|作为|取|为)?\s*"
               r"(?:fdr|padj|adjusted[\s_-]*p|显著性水平)"),
)
_SCORING_WORD = re.compile(
    r"(?i)(?<![A-Za-z0-9_])(?:gsva|ssgsea|plage|zscore)(?![A-Za-z0-9_])|评分|得分|"
    r"pathway\s+score|gene\s+set\s+score")


def _settings(cfg) -> dict:
    value = ((cfg or {}).get("analysis") or {}).get("pathway_scoring")
    return value if isinstance(value, dict) else {}


def _positive_number(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"pathway_scoring.{name} must be a finite number in configuration")
    value = float(value)
    if not low < value <= high:
        raise ValueError(f"pathway_scoring.{name} is outside the allowed range")
    return value


def trigger_hits(request: str, settings: dict | None = None) -> list[str]:
    """Configured trigger words present in the request; deterministic substring match."""
    text = (request or "").casefold()
    words = (settings or {}).get("trigger")
    if not isinstance(words, list) or not words:
        words = list(DEFAULT_TRIGGERS)
    hits = [word.strip() for word in words
            if isinstance(word, str) and word.strip() and word.strip().casefold() in text]
    return list(dict.fromkeys(hits))


def requested_methods(request: str, settings: dict | None = None) -> tuple[str, ...]:
    """Methods the user named explicitly; anything other than exactly one means all."""
    allowed = [m for m in ((settings or {}).get("methods") or DEFAULT_METHODS) if m in METHODS]
    if not allowed:
        allowed = list(DEFAULT_METHODS)
    text = request or ""
    tokens = {match.group(1).lower() for match in _METHOD_TOKEN.finditer(text)}
    named = {token for token in tokens if token != "gsva"}
    if "gsva" in tokens and _GSVA_METHOD_QUALIFIER.search(text):
        named.add("gsva")
    chosen = [method for method in allowed if method in named]
    return tuple(chosen) if len(chosen) == 1 else tuple(allowed)


def plan_declares_scoring(raw_plan: dict) -> bool:
    """True when the plan actually asks for a pathway/gene-set score computation."""
    if not isinstance(raw_plan, dict):
        return False
    texts = [str(raw_plan.get("method") or "")]
    texts += [str(step.get("purpose") or "") for step in raw_plan.get("steps") or []
              if isinstance(step, dict)]
    return any(_SCORING_WORD.search(text) for text in texts)


def _quoted_thresholds(request: str) -> dict[float, str]:
    """Adjusted-p values the request states, with the verbatim quotation of each.

    Only probabilities are accepted, so ``-log10FDR`` style figure annotations can
    never be mistaken for a threshold. A percent sign is honoured because "FDR 5%"
    is an explicit user value, and the quoted text keeps it traceable.
    """
    text = request or ""
    values = {}
    for pattern in _THRESHOLD_PATTERNS:
        for match in pattern.finditer(text):
            if "log" in text[max(0, match.start() - 6):match.start()].lower():
                continue
            try:
                number = float(match.group(1))
            except (IndexError, ValueError):
                continue
            if "%" in match.group(0):
                number = number / 100.0
            if 0 < number < 1:
                values.setdefault(number, match.group(0).strip())
    return values


def _request_thresholds(request: str) -> set[float]:
    return set(_quoted_thresholds(request))


def _resolve_fdr(request: str, settings: dict, parameters, strict: bool):
    """Frozen threshold plus provenance; never model-chosen and never a silent default."""
    declared = [p for p in (parameters or []) if isinstance(p, dict)
                and str(p.get("name") or "") in FDR_PARAMETER_NAMES]
    quotations = _quoted_thresholds(request)
    quoted = set(quotations)
    if len(quoted) > 1:
        raise ValueError(
            "The request states more than one adjusted-p threshold; state exactly one so the "
            "frozen significance level stays unambiguous.")
    if declared:
        if len(declared) > 1:
            raise ValueError("Declare at most one adjusted-p threshold parameter.")
        parameter = declared[0]
        source = str(parameter.get("source") or "")
        if source != "user" and not source.startswith("config:"):
            raise ValueError(
                "The adjusted-p threshold must be quoted from the request or read from "
                "configuration; it cannot be a fixed_method value.")
        text = str(parameter.get("value", "")).strip()
        try:
            value = float(text)
        except (TypeError, ValueError):
            # A user quotation may keep its wording ("FDR 0.05"); the probability it
            # states is the threshold, and it must still be an exact request quotation.
            stated = _request_thresholds(text)
            if len(stated) != 1:
                raise ValueError(
                    "The declared adjusted-p threshold must state exactly one probability.")
            value = next(iter(stated))
        if not 0 < value < 1:
            raise ValueError("The adjusted-p threshold must be strictly between 0 and 1.")
        if quoted and any(abs(value - other) > 1e-12 for other in quoted):
            raise ValueError(
                "The declared adjusted-p threshold disagrees with the threshold written in the "
                "request; quote the request value exactly.")
        return value, source, text
    if quoted:
        value = next(iter(quoted))
        if strict:
            raise ValueError(
                "The request states an adjusted-p threshold but the plan does not declare it. Add "
                "a parameter whose value is this exact quotation, source=user: "
                + repr(quotations[value]))
        return value, "user", quotations[value]
    configured = settings.get("adjusted_p_max")
    if configured is None:
        if strict:
            raise ValueError(
                "No frozen adjusted-p threshold is available: the request states none and "
                "analysis.pathway_scoring.adjusted_p_max is not configured. Add the configuration "
                "key or state the threshold in the request.")
        return None, None, None
    return (_positive_number(configured, "adjusted_p_max", 0.0, 1.0),
            "config:analysis.pathway_scoring.adjusted_p_max",
            str(configured))


def frozen_threshold_parameter(request: str, cfg, parameters) -> dict | None:
    """The traceable parameter a plan must carry for its frozen threshold, if missing.

    A user value is recorded as the verbatim quotation so it can never be a
    model-chosen number; a configured value is recorded with its configuration key.
    """
    declared = [p for p in (parameters or []) if isinstance(p, dict)
                and str(p.get("name") or "") in FDR_PARAMETER_NAMES]
    if declared:
        return None
    quotations = _quoted_thresholds(request)
    if len(quotations) > 1:
        raise ValueError(
            "The request states more than one adjusted-p threshold; state exactly one so the "
            "frozen significance level stays unambiguous.")
    if quotations:
        value = next(iter(quotations))
        return {"name": "pathway_adjusted_p_max", "value": quotations[value], "source": "user"}
    configured = _settings(cfg).get("adjusted_p_max")
    if configured is None:
        return None
    return {"name": "pathway_adjusted_p_max", "value": str(configured),
            "source": "config:analysis.pathway_scoring.adjusted_p_max"}


def _limits(settings):
    minimum = settings.get("min_size", 5)
    maximum = settings.get("max_size", 500)
    if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 1:
        raise ValueError("pathway_scoring.min_size must be a positive integer")
    if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum <= minimum:
        raise ValueError("pathway_scoring.max_size must be an integer above min_size")
    return minimum, maximum


def _consensus_min(settings, methods):
    value = settings.get("consensus_min_methods", 2)
    if len(methods) < 2:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 2 <= value <= len(methods):
        raise ValueError(
            "pathway_scoring.consensus_min_methods must be an integer between 2 and the number "
            "of scored methods")
    return value


def _seed(settings):
    value = settings.get("seed")
    return value if isinstance(value, int) and not isinstance(value, bool) else 2024


def draft_protocol(request: str, cfg, parameters=None) -> dict | None:
    """Prompt-side protocol: no binding-derived test yet; ``None`` when not triggered."""
    settings = _settings(cfg)
    hits = trigger_hits(request, settings)
    if not hits:
        return None
    methods = requested_methods(request, settings)
    fdr, source, quotation = _resolve_fdr(request, settings, parameters, strict=False)
    minimum, maximum = _limits(settings)
    return {"trigger": hits, "methods": list(methods),
            "param_api": {m: PARAM_API[m] for m in methods},
            "min_size": minimum, "max_size": maximum,
            "fdr": fdr, "fdr_source": source, "fdr_quotation": quotation,
            "test": None, "contrast": {"numerator": "unknown", "denominator": "unknown"},
            "contrast_known": False,
            "direction": "unknown",
            "multiple_testing": "BH within method family",
            "consensus_min_methods": _consensus_min(settings, methods),
            "seed": _seed(settings), "score_meaning": SCORE_MEANING,
            "outputs": dict(OUTPUTS)}


def _frozen_test(bindings):
    matrices = (bindings or {}).get("matrices") or []
    if not isinstance(matrices, list) or len(matrices) != 1:
        raise ValueError(
            "Pathway scoring requires exactly one bound expression matrix with explicit group "
            "metadata; scoring several matrices needs a separate approved task.")
    binding = matrices[0]
    return ("paired_wilcoxon" if binding.get("pair_key") else "unpaired_mannwhitney"), binding


def _frozen_contrast(research, binding):
    """Use the approved contrast when stated; otherwise record an unusable direction."""
    mapped = sorted({str(value) for value in (binding.get("group_map") or {}).values()})
    if len(mapped) != 2:
        raise ValueError(
            "Pathway scoring requires exactly two mapped groups in input_bindings.matrices[0].")
    context = (research or {}).get("context") if isinstance(research, dict) else None
    contrast = (context or {}).get("contrast") or {}
    numerator = str(contrast.get("numerator") or "unknown").strip()
    denominator = str(contrast.get("denominator") or "unknown").strip()
    if "unknown" not in {numerator, denominator} and {numerator, denominator} <= set(mapped):
        return {"numerator": numerator, "denominator": denominator}, True, "approved_research_contrast"
    if "unknown" not in {numerator, denominator}:
        raise ValueError(
            "The research contrast labels must be the mapped group labels of the bound matrix: "
            + ", ".join(mapped))
    # No approved direction: the labels only keep the arithmetic well defined. Two-sided
    # tests are symmetric, so the p-value and |effect| stay valid, but the sign is arbitrary
    # and must never be interpreted as a biological direction.
    return ({"numerator": mapped[0], "denominator": mapped[1]}, False,
            "binding_order_unverified_direction")


def _trusted_gene_set_universe(protocol: dict, bindings, inspection) -> tuple[list[dict], list[dict]]:
    """Derive the complete gene-set audit universe from trusted inspected inputs.

    The score table alone cannot prove that a pathway was not silently omitted, and
    exported tables cannot prove their own filter reason. This derivation reads the
    inspected gene-set members and the inspected expression feature names (both kept
    as hashes locally) so the approved plan freezes:
    - every resource set name and resource size;
    - its measured size in the scored (variance-filtered) matrix;
    - its filter status under the frozen min/max thresholds.

    ``size_in_matrix`` counts members present in the matrix after the protocol's
    zero-variance feature filter, matching what GSVA receives. Status precedence is
    ``zero_overlap`` -> ``no_variable_features`` -> ``size_below_min`` ->
    ``size_above_max`` -> ``used``.
    """
    matrices = (bindings or {}).get("matrices") or []
    if len(matrices) != 1:
        raise ValueError("A frozen gene-set universe needs exactly one bound expression matrix")
    binding = matrices[0]
    resources = (bindings or {}).get("gene_sets") or []
    if not isinstance(resources, list) or not resources:
        raise ValueError("A frozen gene-set universe needs a bound gene-set resource")
    profiles = {str(profile.get("path")): profile for profile in (inspection or {}).get("profiles", [])}
    matrix = profiles.get(str(binding.get("path")))
    if not matrix:
        raise ValueError("Trusted inspection lacks the bound expression matrix")
    axis = "rows" if binding.get("sample_axis") == "columns" else "columns"
    features = matrix.get("keys", {}).get(axis, {}).get("values")
    features = features if isinstance(features, list) else ([] if features is None else [features])
    constant_key = "constant_row_sha256" if axis == "rows" else "constant_column_sha256"
    constant = matrix.get(constant_key)
    if not isinstance(constant, list):
        raise ValueError(
            "Trusted inspection lacks zero-variance feature identities; re-inspect the inputs "
            "before approving pathway scoring")
    feature_set = {str(value) for value in features}
    constant_set = {str(value) for value in constant}
    minimum, maximum = int(protocol["min_size"]), int(protocol["max_size"])

    universe, seen = [], set()
    resource_records = []
    for path in resources:
        profile = profiles.get(str(path))
        if not profile or "gene_sets" not in profile:
            raise ValueError(f"Trusted inspection lacks the gene-set resource: {path}")
        names = profile.get("set_name_sha256")
        sizes = profile.get("gene_sets", {}).get("sizes")
        members = profile.get("set_members")
        if (not isinstance(names, list) or not isinstance(sizes, list) or
                not isinstance(members, list) or not (len(names) == len(sizes) == len(members))):
            raise ValueError(f"Gene-set resource inspection is incomplete: {path}")
        resource_records.append({"path": str(path), "sha256": profile.get("sha256")})
        for name_hash, resource_size, member_list in zip(names, sizes, members):
            name_hash = str(name_hash)
            if name_hash in seen:
                raise ValueError("Gene-set names are duplicated across bound resources")
            seen.add(name_hash)
            if not isinstance(member_list, list):
                raise ValueError(f"Gene-set member inspection is invalid: {path}")
            members_hash = {str(value) for value in member_list}
            overlap_all = len(members_hash & feature_set)
            overlap_variable = len(members_hash & (feature_set - constant_set))
            if overlap_all == 0:
                status = "zero_overlap"
            elif overlap_variable == 0:
                status = "no_variable_features"
            elif overlap_variable < minimum:
                status = "size_below_min"
            elif overlap_variable > maximum:
                status = "size_above_max"
            else:
                status = "used"
            universe.append({"gene_set_sha256": name_hash,
                             "size_in_resource": int(resource_size),
                             "size_in_matrix": int(overlap_variable),
                             "status": status})
    universe.sort(key=lambda item: item["gene_set_sha256"])
    resource_records.sort(key=lambda item: item["path"])
    return universe, resource_records


def frozen_protocol(request: str, cfg, parameters, bindings, research, inspection=None) -> dict | None:
    """Strict protocol frozen into the approved plan; raises instead of guessing."""
    protocol = draft_protocol(request, cfg, parameters)
    if protocol is None:
        return None
    fdr, source, quotation = _resolve_fdr(request, _settings(cfg), parameters, strict=True)
    protocol.update(fdr=fdr, fdr_source=source, fdr_quotation=quotation)
    test, binding = _frozen_test(bindings)
    contrast, known, source_labels = _frozen_contrast(research, binding)
    protocol.update(test=test, contrast=contrast, contrast_known=known,
                    contrast_labels_source=source_labels,
                    direction=("numerator_minus_denominator" if known else "unknown"))
    if inspection is not None:
        universe, resources = _trusted_gene_set_universe(protocol, bindings, inspection)
        protocol.update(gene_set_universe=universe, gene_set_resources=resources)
    return protocol


def enforce(protocol: dict, raw_plan: dict) -> list[str]:
    """Every protocol output must be declared, and the scores must come from an R step."""
    problems = []
    steps = [step for step in (raw_plan.get("steps") or []) if isinstance(step, dict)]
    declared = {str(output) for step in steps for output in (step.get("outputs") or [])}
    for name, path in protocol["outputs"].items():
        if path not in declared:
            problems.append(f"declare the pathway-scoring output {path} ({name})")
    if not any(step.get("language") == "r" for step in steps):
        problems.append("pathway scoring needs at least one R step that calls GSVA")
    elif protocol["outputs"]["scores"] not in {str(output) for step in steps
                                               if step.get("language") == "r"
                                               for output in (step.get("outputs") or [])}:
        problems.append("produce " + protocol["outputs"]["scores"] + " from an R step")
    gene_sets = (raw_plan.get("input_bindings") or {}).get("gene_sets")
    if not isinstance(gene_sets, list) or not gene_sets:
        problems.append("bind the gene-set resource under input_bindings.gene_sets so its "
                        "coverage is inspected before approval")
    return problems


def prompt_block(protocol: dict) -> str:
    """Deterministic instructions appended to the planning prompt when triggered."""
    fdr = protocol["fdr"]
    fdr_text = ("quoted from the request or read from configuration" if fdr is None
                else f"{fdr} [{protocol['fdr_source']}]")
    outputs = "; ".join(f"{name}={path}" for name, path in protocol["outputs"].items())
    return (
        "PATHWAY-SCORING PROTOCOL (frozen locally, not negotiable). Trigger words: "
        + ", ".join(protocol["trigger"]) + ". Score with the GSVA package parameter objects for: "
        + ", ".join(protocol["methods"]) + " ("
        + ", ".join(protocol["param_api"][m] for m in protocol["methods"]) + "). "
        "Gene-set size limits " + str(protocol["min_size"]) + "-" + str(protocol["max_size"]) + ". "
        "Frozen adjusted-p threshold: " + fdr_text + ". "
        "Drop features with zero variance across samples before scoring and record how many were "
        "dropped; otherwise plage, zscore and gsva fail outright. "
        "Audit every resource gene set exactly once: size_in_resource is the resource size, "
        "size_in_matrix is the member count in the variance-filtered scored matrix, and status "
        "precedence is zero_overlap, no_variable_features, size_below_min, size_above_max, used. "
        "A filtered set needs a nonempty reason; a used set must have an empty reason. "
        "Keep every tested pathway, including negative and not-estimable rows. "
        "Never average, rank-combine or z-combine scores across methods: the scales differ. "
        "Required outputs: " + outputs + ". "
        "Bind at most tables/gsva_significant_pathways.csv as pathway_enrichment evidence with "
        "entity_type=pathway, evidence_context=derived_estimate, measured_omics=[], "
        "columns entity_id=gene_set, adjusted_p=min_padj, n=n_units, selected=selected, "
        "status=status, row_key [gene_set], effect_scale unknown and no effect threshold. "
        "Do not add another FDR threshold: the frozen value above is the only one."
    )


def render_lines(protocol: dict, language: str = "en") -> list[str]:
    from multiomics_tui.language import choose

    def t(en, zh):
        return choose(language, en, zh)

    fdr = protocol["fdr"]
    fdr_text = (f"{fdr} ({protocol['fdr_source']})" if fdr is not None
                else t("must be quoted or configured", "须引用或配置"))
    test = protocol.get("test") or t("from binding", "按绑定确定")
    consensus = protocol.get("consensus_min_methods")
    consensus_text = (t(f">= {consensus} methods agree", f">= {consensus} 种方法一致")
                      if consensus else t("single method; no consensus rule", "单方法；无共识判定"))
    contrast = protocol.get("contrast") or {}
    if protocol.get("contrast_known"):
        direction = f"{contrast.get('numerator')} - {contrast.get('denominator')}"
    else:
        direction = t("unknown (no explicit contrast)", "未知（无显式对比方向）")
    return [
        t("Trigger: ", "触发词：") + ", ".join(protocol["trigger"]),
        t("GSVA methods: ", "GSVA 方法：") + ", ".join(protocol["methods"]),
        t("Adjusted-p threshold: ", "校正 p 阈值：") + fdr_text,
        t("Group test: ", "组间检验：") + f"{test}; " + t("direction ", "方向 ") + direction,
        t("Cross-method consensus: ", "跨方法一致性：") + consensus_text,
        t("Score meaning: ", "分数含义：") + SCORE_MEANING,
    ]


def _read_table(path: Path, columns, name: str, allow_empty: bool = False):
    import pandas as pd
    if not path.is_file():
        raise ValueError(f"Pathway-scoring output is missing: {name}")
    frame = pd.read_csv(path)
    missing = [column for column in columns if column not in frame.columns]
    if missing:
        raise ValueError(f"{name} lacks required columns: " + ", ".join(missing))
    if frame.empty and not allow_empty:
        raise ValueError(f"{name} is empty")
    return frame


def _flag(value) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "1"}:
        return True
    if text in {"false", "0"}:
        return False
    raise ValueError("Pathway-scoring boolean column holds a non-boolean value: " + str(value))


def _is_missing(value) -> bool:
    if value is None:
        return True
    try:
        return bool(math.isnan(float(value)))
    except (TypeError, ValueError):
        return isinstance(value, str) and not value.strip()


def _close(recorded, recomputed, name):
    if recomputed is None:
        if not _is_missing(recorded):
            raise ValueError(f"Pathway-scoring {name} must be empty for a not-estimable row")
        return
    if _is_missing(recorded):
        raise ValueError(f"Pathway-scoring {name} is missing for a tested row")
    if not math.isclose(float(recorded), float(recomputed), rel_tol=TOLERANCE, abs_tol=TOLERANCE):
        raise ValueError(f"Pathway-scoring {name} does not reproduce: recorded {recorded} vs "
                         f"recomputed {recomputed}")


def _hash(value: str) -> str:
    return hashlib.sha256(str(value).encode()).hexdigest()


def _sample_groups(root, plan):
    from multiomics_tui.output_contracts import binding_data
    binding = binding_data(root, plan, 0)
    return {key: (str(group), pair) for key, (group, pair) in binding.items()}


def _recompute(values: dict, groups: dict, protocol: dict):
    """Recompute one (method, gene-set) row from the sample-level scores."""
    import numpy as np
    from scipy import stats as scipy_stats

    numerator = protocol["contrast"]["numerator"]
    denominator = protocol["contrast"]["denominator"]
    paired = protocol["test"] == "paired_wilcoxon"
    upper, lower = {}, {}
    for sample_id, score in values.items():
        key = _hash(sample_id)
        if key not in groups:
            raise ValueError("Score table contains a sample absent from the frozen binding")
        group, pair = groups[key]
        if group == numerator:
            upper[sample_id] = (float(score), pair)
        elif group == denominator:
            lower[sample_id] = (float(score), pair)
        else:
            raise ValueError("Score table sample has a group outside the frozen contrast")
    if paired:
        pairs = {}
        for sample_id, (score, pair) in upper.items():
            pairs.setdefault(pair, {})["numerator"] = score
        for sample_id, (score, pair) in lower.items():
            pairs.setdefault(pair, {})["denominator"] = score
        deltas = []
        for pair in pairs.values():
            if set(pair) != {"numerator", "denominator"}:
                raise ValueError("A frozen pair lacks one of the two contrast groups")
            deltas.append(pair["numerator"] - pair["denominator"])
        n_units = len(deltas)
        counts = {"n_units": n_units, "n_numerator": len(upper), "n_denominator": len(lower)}
        if not deltas:
            raise ValueError("Score table has no complete pair for the frozen contrast")
        delta = np.asarray(deltas, dtype=float)
        if not np.isfinite(delta).all():
            return {"status": NOT_ESTIMABLE, "reason": "nonfinite_estimate", **counts}
        if n_units < MIN_UNITS:
            return {"status": NOT_ESTIMABLE, "reason": "fewer_than_three_units", **counts}
        if np.all(delta == 0):
            return {"status": NOT_ESTIMABLE, "reason": "zero_difference", **counts}
        effect = float(delta.mean())
        deviation = float(delta.std(ddof=1)) / math.sqrt(n_units)
        low, high = scipy_stats.t.interval(0.95, n_units - 1, loc=effect, scale=deviation)
        ranks = scipy_stats.wilcoxon(delta, alternative="two-sided", correction=True,
                                     method="approx")
        return {"status": TESTED, "reason": None, **counts, "effect": effect,
                "ci_low": float(low), "ci_high": float(high), "pvalue": float(ranks.pvalue)}
    upper_values = np.asarray([score for score, _ in upper.values()], dtype=float)
    lower_values = np.asarray([score for score, _ in lower.values()], dtype=float)
    if not upper_values.size or not lower_values.size:
        raise ValueError("Score table lacks one of the two contrast groups")
    counts = {"n_units": int(min(upper_values.size, lower_values.size)),
              "n_numerator": int(upper_values.size), "n_denominator": int(lower_values.size)}
    if not (np.isfinite(upper_values).all() and np.isfinite(lower_values).all()):
        return {"status": NOT_ESTIMABLE, "reason": "nonfinite_estimate", **counts}
    if counts["n_units"] < MIN_UNITS:
        return {"status": NOT_ESTIMABLE, "reason": "fewer_than_three_units", **counts}
    if (np.all(upper_values == upper_values[0]) and np.all(lower_values == lower_values[0]) and
            upper_values[0] == lower_values[0]):
        return {"status": NOT_ESTIMABLE, "reason": "zero_difference", **counts}
    effect = float(upper_values.mean() - lower_values.mean())
    deviation = math.sqrt(float(upper_values.var(ddof=1)) / upper_values.size +
                          float(lower_values.var(ddof=1)) / lower_values.size)
    welch = scipy_stats.ttest_ind(upper_values, lower_values, equal_var=False)
    low, high = scipy_stats.t.interval(0.95, float(welch.df), loc=effect, scale=deviation)
    ranks = scipy_stats.mannwhitneyu(upper_values, lower_values, alternative="two-sided",
                                     method="asymptotic", use_continuity=True)
    return {"status": TESTED, "reason": None, **counts, "effect": effect,
            "ci_low": float(low), "ci_high": float(high), "pvalue": float(ranks.pvalue)}


def _bh(frame):
    """BH inside each declared family; the protocol declares one family per method."""
    import numpy as np
    from scipy.stats import false_discovery_control
    adjusted = np.full(len(frame), float("nan"))
    for family, block in frame.groupby("family", sort=False):
        tested = block.status.eq(TESTED)
        if not tested.any():
            continue
        pvalues = block.loc[tested, "pvalue"].to_numpy(dtype=float)
        if not np.isfinite(pvalues).all():
            raise ValueError("A tested pathway row carries a nonfinite p-value")
        adjusted[block.index[tested.to_numpy()]] = false_discovery_control(pvalues, method="bh")
    return adjusted


def verify_outputs(root, task_dir, plan: dict) -> dict:
    """Recompute the whole pathway-scoring contract from the sample-level scores."""
    import numpy as np

    protocol = plan.get("pathway_protocol")
    if not isinstance(protocol, dict):
        raise ValueError("Plan has no frozen pathway-scoring protocol")
    root, task_dir = Path(root), Path(task_dir)
    paths = {name: task_dir / path for name, path in protocol["outputs"].items()}
    groups = _sample_groups(root, plan)
    methods = list(protocol["methods"])
    fdr = float(protocol["fdr"])
    contrast_known = bool(protocol.get("contrast_known"))
    if not protocol.get("gene_set_universe"):
        raise ValueError(
            "The approved plan lacks a frozen gene-set universe; re-plan pathway scoring "
            "with the current trusted input inspection")
    reference = plan.get("input_inspection") or {}
    inspection_root = (root / ".runtime/input_inspections").resolve()
    inspection_path = (root / str(reference.get("path") or "")).resolve()
    if not inspection_path.is_relative_to(inspection_root) or not inspection_path.is_file():
        raise ValueError("Pathway scoring has no trusted input inspection")
    if hashlib.sha256(inspection_path.read_bytes()).hexdigest() != reference.get("sha256"):
        raise ValueError("Input inspection changed after approval")
    inspection = json.loads(inspection_path.read_text(encoding="utf-8"))
    expected_universe, expected_resources = _trusted_gene_set_universe(
        protocol, plan.get("input_bindings"), inspection)
    if expected_universe != protocol["gene_set_universe"]:
        raise ValueError("Frozen gene-set universe differs from the trusted input inspection")
    if expected_resources != protocol.get("gene_set_resources"):
        raise ValueError("Frozen gene-set resources differ from the trusted input inspection")
    universe = {record["gene_set_sha256"]: record for record in expected_universe}
    used_hashes = {record["gene_set_sha256"] for record in expected_universe
                   if record["status"] == "used"}
    if not used_hashes:
        raise ValueError("The frozen gene-set resource has no scorable set")

    scores = _read_table(paths["scores"], SCORES_COLUMNS, "gsva_scores.csv")
    for column in ("method", "gene_set", "sample_id"):
        scores[column] = scores[column].astype(str)
    if set(scores.method) != set(methods):
        raise ValueError("Score table methods differ from the frozen protocol: "
                         + ", ".join(sorted(set(scores.method))))
    if scores.duplicated(["method", "gene_set", "sample_id"]).any():
        raise ValueError("Score table repeats a (method, gene_set, sample) key")
    if not np.isfinite(scores.score.to_numpy(dtype=float)).all():
        raise ValueError("Score table carries a nonfinite score")
    if not {_hash(sample) for sample in scores.sample_id} <= set(groups):
        raise ValueError("Score table samples do not match the frozen binding")
    gene_sets = sorted(set(scores.gene_set))
    scored_hashes = {_hash(name) for name in gene_sets}
    if scored_hashes != used_hashes:
        missing = sorted(used_hashes - scored_hashes)
        extra = sorted(scored_hashes - used_hashes)
        raise ValueError(
            "Score table gene sets differ from the frozen gene-set universe; missing="
            f"{missing[:5]}, unexpected={extra[:5]}")
    for method in methods:
        block = scores.loc[scores.method == method]
        if set(block.sample_id.map(_hash)) != set(groups):
            raise ValueError(f"Method {method} silently dropped samples")
        if set(block.gene_set) != set(gene_sets):
            raise ValueError(f"Method {method} scored a different gene-set set")
    values = {}
    for (method, gene_set), block in scores.groupby(["method", "gene_set"], sort=False):
        values[(method, gene_set)] = dict(zip(block.sample_id, block.score))

    tests = _read_table(paths["tests"], TESTS_COLUMNS, "gsva_group_tests.csv")
    for column in ("method", "gene_set", "family", "test", "status", "reason",
                   "effect_ci_method", "pvalue_method", "direction"):
        tests[column] = tests[column].astype(str)
    if tests.duplicated(["method", "gene_set"]).any():
        raise ValueError("Group-test table repeats a (method, gene_set) key")
    if set(tests.method) != set(methods):
        raise ValueError("Group-test methods differ from the frozen protocol")
    if set(tests.gene_set) != set(gene_sets):
        raise ValueError("Group-test gene sets differ from the scored gene sets")
    for method in methods:
        family = "pathway_scoring_" + method
        if not tests.loc[tests.method == method, "family"].eq(family).all():
            raise ValueError(f"Group-test family must be {family}")
    if not tests.test.eq(protocol["test"]).all():
        raise ValueError("Group-test test differs from the frozen protocol")
    if not tests.direction.eq(protocol["direction"]).all():
        raise ValueError("Group-test direction differs from the frozen protocol")
    if not tests.contrast_numerator.eq(protocol["contrast"]["numerator"]).all() or \
            not tests.contrast_denominator.eq(protocol["contrast"]["denominator"]).all():
        raise ValueError("Group-test contrast differs from the frozen protocol")
    if not set(tests.status) <= {TESTED, NOT_ESTIMABLE}:
        raise ValueError("Group-test status holds an unexpected value")
    expected_ci = "paired_t_mean_ci" if protocol["test"] == "paired_wilcoxon" else "welch_t_mean_ci"
    expected_p = ("wilcoxon_signed_rank" if protocol["test"] == "paired_wilcoxon"
                  else "mann_whitney_u")
    if not tests.effect_ci_method.eq(expected_ci).all():
        raise ValueError(f"Group-test effect_ci_method must be {expected_ci}")
    if not tests.pvalue_method.eq(expected_p).all():
        raise ValueError(f"Group-test pvalue_method must be {expected_p}")

    recorded = {name: tests[name].to_numpy() for name in
                ("n_units", "n_numerator", "n_denominator", "effect", "ci_low", "ci_high",
                 "pvalue", "padj")}
    for index, row in enumerate(tests.itertuples(index=False)):
        recomputed = _recompute(values[(row.method, row.gene_set)], groups, protocol)
        label = f"{row.method}/{row.gene_set}"
        for name in ("n_units", "n_numerator", "n_denominator"):
            if int(row._asdict()[name]) != recomputed[name]:
                raise ValueError(f"Group-test {name} differs from recomputation for {label}: "
                                 f"{row._asdict()[name]} vs {recomputed[name]}")
        if recomputed["status"] != row.status:
            raise ValueError(f"Group-test status differs from recomputation for {label}: "
                             f"{row.status} vs {recomputed['status']}")
        if row.status == NOT_ESTIMABLE:
            if row.reason not in REASONS:
                raise ValueError("Group-test not-estimable reason is outside the declared vocabulary")
            if row.reason != recomputed["reason"]:
                raise ValueError(f"Group-test reason differs from recomputation for {label}: "
                                 f"{row.reason} vs {recomputed['reason']}")
            for name in ("effect", "ci_low", "ci_high", "pvalue", "padj"):
                if not _is_missing(recorded[name][index]):
                    raise ValueError(f"Not-estimable row carries a {name} value: {label}")
            if _flag(row.selected):
                raise ValueError("Not-estimable row cannot be selected")
            continue
        if not _is_missing(row.reason) and row.reason not in {"", "nan", "None"}:
            raise ValueError(f"Tested row carries a not-estimable reason: {label}")
        for name in ("effect", "ci_low", "ci_high", "pvalue"):
            _close(recorded[name][index], recomputed[name], f"{label} {name}")
        if row.n_units < MIN_UNITS:
            raise ValueError("Tested row has fewer than three independent units")
        if not (row.ci_low <= row.effect <= row.ci_high):
            raise ValueError("Group-test effect lies outside its own confidence interval")

    expected_padj = _bh(tests)
    for index, value in enumerate(expected_padj):
        if tests.status.iloc[index] == NOT_ESTIMABLE:
            continue
        _close(recorded["padj"][index], float(value), f"{tests.gene_set.iloc[index]} padj")
    finite_padj = expected_padj[np.isfinite(expected_padj)]
    if finite_padj.size and (finite_padj.min() < 0 or finite_padj.max() > 1):
        raise ValueError("Recomputed padj is outside [0,1]")
    expected_selected = tests.status.eq(TESTED) & np.less_equal(expected_padj, fdr)
    if not np.array_equal(tests.selected.map(_flag).to_numpy(), expected_selected.to_numpy()):
        raise ValueError("Group-test selected does not follow the frozen adjusted-p threshold")
    if tests.n_units.nunique() != 1:
        raise ValueError("Independent unit counts differ between methods in one protocol")
    units = int(tests.n_units.iloc[0])

    significant = _read_table(paths["significant"], SIGNIFICANT_COLUMNS,
                              "gsva_significant_pathways.csv")
    significant["gene_set"] = significant.gene_set.astype(str)
    if significant.duplicated(["gene_set"]).any():
        raise ValueError("Significant table repeats a gene_set")
    if set(significant.gene_set) != set(gene_sets):
        raise ValueError("Significant table must cover every scored pathway exactly once")
    consensus_min = protocol.get("consensus_min_methods")
    for row in significant.itertuples(index=False):
        block = tests.loc[tests.gene_set == row.gene_set]
        tested = block.loc[block.status == TESTED]
        selected = block.loc[block.selected.map(_flag)]
        if int(row.methods_tested) != len(tested):
            raise ValueError(f"Significant methods_tested differs for {row.gene_set}")
        if int(row.methods_significant) != len(selected):
            raise ValueError(f"Significant methods_significant differs for {row.gene_set}")
        expected_names = ";".join(sorted(selected.method))
        recorded_names = "" if _is_missing(row.significant_methods) else str(row.significant_methods)
        if recorded_names != expected_names:
            raise ValueError(f"Significant significant_methods differs for {row.gene_set}")
        if int(row.n_units) != units:
            raise ValueError(f"Significant n_units differs for {row.gene_set}")
        if not tested.shape[0]:
            if row.status != NOT_ESTIMABLE:
                raise ValueError(f"Pathway without any estimable method must be not_estimable: "
                                 f"{row.gene_set}")
            expected_reason = ";".join(sorted(set(block.reason)))
            if str(row.reason) != expected_reason:
                raise ValueError(f"Significant reason differs for {row.gene_set}")
            for name in ("min_padj", "max_abs_effect"):
                if not _is_missing(getattr(row, name)):
                    raise ValueError(f"Not-estimable pathway carries {name}: {row.gene_set}")
            if _flag(row.selected) or _flag(row.consensus):
                raise ValueError(f"Not-estimable pathway cannot be selected: {row.gene_set}")
            continue
        if row.status != TESTED:
            raise ValueError(f"Pathway with an estimable method must be tested: {row.gene_set}")
        if not _is_missing(row.reason) and row.reason not in {"", "nan", "None"}:
            raise ValueError(f"Tested pathway carries a not-estimable reason: {row.gene_set}")
        _close(row.min_padj, float(tested.padj.min()), f"{row.gene_set} min_padj")
        _close(row.max_abs_effect, float(tested.effect.abs().max()),
               f"{row.gene_set} max_abs_effect")
        effects = tested.effect.to_numpy(dtype=float)
        expected_consistent = (None if (len(effects) < 2 or not contrast_known)
                               else bool((effects > 0).all() or (effects < 0).all()))
        if expected_consistent is None:
            if not _is_missing(row.direction_consistent):
                raise ValueError(f"direction_consistent must be empty for {row.gene_set}")
        elif _flag(row.direction_consistent) != expected_consistent:
            raise ValueError(f"direction_consistent differs for {row.gene_set}")
        expected_selected_row = float(tested.padj.min()) <= fdr
        if _flag(row.selected) != expected_selected_row:
            raise ValueError(f"Significant selected differs for {row.gene_set}")
        direction_ok = True if expected_consistent is None else expected_consistent
        expected_consensus = (bool(consensus_min) and expected_selected_row and
                              len(selected) >= int(consensus_min or 0) and direction_ok)
        if _flag(row.consensus) != expected_consensus:
            raise ValueError(f"Significant consensus differs for {row.gene_set}")

    audit = _read_table(paths["gene_sets"], AUDIT_COLUMNS, "gsva_gene_set_audit.csv")
    audit["gene_set"] = audit.gene_set.astype(str)
    audit["gene_set_sha256"] = audit.gene_set.map(_hash)
    if audit.duplicated(["gene_set_sha256"]).any():
        raise ValueError("Gene-set audit repeats a gene_set")
    if not set(audit.status) <= AUDIT_STATUSES:
        raise ValueError("Gene-set audit status is outside the declared vocabulary")
    if set(audit.gene_set_sha256) != set(universe):
        missing = sorted(set(universe) - set(audit.gene_set_sha256))
        extra = sorted(set(audit.gene_set_sha256) - set(universe))
        raise ValueError("Gene-set audit does not cover the frozen resource universe exactly; "
                         f"missing={missing[:5]}, unexpected={extra[:5]}")
    for row in audit.itertuples(index=False):
        expected = universe[row.gene_set_sha256]
        if int(row.size_in_resource) != expected["size_in_resource"]:
            raise ValueError(f"Gene-set audit size_in_resource differs: {row.gene_set}")
        if int(row.size_in_matrix) != expected["size_in_matrix"]:
            raise ValueError(f"Gene-set audit size_in_matrix differs: {row.gene_set}")
        if str(row.status) != expected["status"]:
            raise ValueError(f"Gene-set audit status differs from trusted filtering: {row.gene_set}")
        if expected["status"] == "used":
            if not _is_missing(row.reason):
                raise ValueError(f"Used gene set carries a filter reason: {row.gene_set}")
        elif _is_missing(row.reason):
            raise ValueError(f"Filtered gene set lacks a filter reason: {row.gene_set}")
    if {row.gene_set_sha256 for row in audit.itertuples(index=False)
            if row.status == "used"} != used_hashes:
        raise ValueError("Gene-set audit 'used' rows differ from the frozen used universe")
    if audit.size_in_matrix.gt(audit.size_in_resource).any():
        raise ValueError("Gene-set audit matrix size exceeds the resource size")
    if audit.loc[audit.status == "used", "size_in_matrix"].lt(protocol["min_size"]).any():
        raise ValueError("A used gene set is below the frozen minSize")

    report = json.loads(paths["protocol"].read_text(encoding="utf-8"))
    for key, expected in (("methods", methods), ("fdr", fdr),
                          ("min_size", protocol["min_size"]), ("max_size", protocol["max_size"]),
                          ("test", protocol["test"]), ("contrast", protocol["contrast"]),
                          ("contrast_known", contrast_known),
                          ("contrast_labels_source", protocol["contrast_labels_source"]),
                          ("seed", protocol["seed"]),
                          ("consensus_min_methods", protocol.get("consensus_min_methods"))):
        if report.get(key) != expected:
            raise ValueError(f"gsva_protocol.json {key} differs from the frozen protocol")
    for key in ("gsva_version", "r_version", "session_info"):
        if not isinstance(report.get(key), str) or not report[key].strip():
            raise ValueError(f"gsva_protocol.json lacks {key}")
    dropped = report.get("dropped_features")
    if isinstance(dropped, bool) or not isinstance(dropped, int) or dropped < 0:
        raise ValueError("gsva_protocol.json dropped_features must be a nonnegative integer")
    for key in ("dropped_gene_sets", "warnings"):
        if not isinstance(report.get(key), list):
            raise ValueError(f"gsva_protocol.json {key} must be a list")

    tested_rows = int(tests.status.eq(TESTED).sum())
    return {"status": "passed", "methods": methods, "gene_sets": len(gene_sets),
            "samples": len(set(scores.sample_id)), "n_units": units,
            "statistical_status": "tested" if tested_rows else "not_estimable",
            "tested_rows": tested_rows,
            "not_estimable_rows": int(tests.status.eq(NOT_ESTIMABLE).sum()),
            "selected_rows": int(expected_selected.sum()),
            "significant_pathways": len(significant),
            "consensus_pathways": int(significant.consensus.map(_flag).sum()),
            "fdr": fdr, "contrast_known": contrast_known, "dropped_features": dropped,
            "verifier": "recomputed rank tests, BH, selection and consensus from sample scores"}

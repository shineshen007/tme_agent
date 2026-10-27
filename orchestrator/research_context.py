"""Explicit, source-verified research context shared by task and legacy runners.

Adapters only project approved column bindings. They never infer study labels,
significance thresholds, entity annotations or independence from file names.
"""
from __future__ import annotations

import copy
import csv
from dataclasses import asdict, dataclass, field
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re

import jsonschema


SCHEMAS = Path(__file__).resolve().parents[1] / "schemas"
MISSING = {"", "na", "nan", "null", "none", "n/a"}
MEASURED_OMICS = {"transcriptomics", "proteomics", "metabolomics", "epigenomics", "genomics", "scrna", "spatial"}
COLUMN_KEYS = {"entity_id", "symbol", "effect", "p_value", "adjusted_p", "n", "selected", "status"}
SCALES = {"log2_fold_change", "log_fold_change", "fold_change", "NES", "odds_ratio", "hazard_ratio", "difference", "unknown"}


def _hash_bytes(content):
    return hashlib.sha256(content).hexdigest()


def _hash_json(value):
    return _hash_bytes(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode())


def _relative(value):
    if (not isinstance(value, str) or not value or "\\" in value or
            PurePosixPath(value).is_absolute() or any(p in {".", "..", ""} for p in value.split("/")) or
            ":" in value or "\x00" in value):
        raise ValueError(f"Unsafe research artifact path: {value!r}")
    return value


def _path(root, relative):
    root = Path(root).resolve()
    path = root / _relative(relative)
    # Reject symlinks, including links that happen to point inside the workspace.
    if any(p.is_symlink() for p in [path, *list(path.parents)[:len(PurePosixPath(relative).parts) - 1]]):
        raise ValueError(f"Symlink research artifact is not allowed: {relative}")
    resolved = path.resolve()
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise ValueError(f"Missing or external research artifact: {relative}")
    return resolved


def _read(root, relative, expected=None):
    path = _path(root, relative)
    data = path.read_bytes()
    digest = _hash_bytes(data)
    if expected is not None and digest != expected:
        raise ValueError(f"Research source hash mismatch: {relative}")
    return data, {"path": relative, "sha256": digest, "bytes": len(data)}


def _text(value, name, default="unknown"):
    if value is None:
        return default
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Research {name} must be a nonempty string")
    return value.strip()


def _context(value, analysis_unit=None):
    if not isinstance(value, dict):
        raise ValueError("Research context must be an object")
    keys = {"question", "disease", "species", "tissue", "analysis_unit", "cohort_id", "contrast", "limitations"}
    if set(value) - keys:
        raise ValueError("Unknown research context fields: " + ", ".join(sorted(set(value) - keys)))
    result = {key: _text(value.get(key), key) for key in keys - {"contrast", "limitations"}}
    if analysis_unit is not None:
        if value.get("analysis_unit") not in {None, analysis_unit}:
            raise ValueError("Research analysis_unit disagrees with approved plan")
        result["analysis_unit"] = _text(analysis_unit, "analysis_unit")
    contrast = value.get("contrast") or {}
    if not isinstance(contrast, dict) or set(contrast) - {"numerator", "denominator"}:
        raise ValueError("Contrast requires numerator and denominator labels")
    result["contrast"] = {key: _text(contrast.get(key), key) for key in ("numerator", "denominator")}
    if (result["contrast"]["numerator"] == result["contrast"]["denominator"] and
            result["contrast"]["numerator"] != "unknown"):
        raise ValueError("Contrast labels must be different")
    limitations = value.get("limitations", [])
    if not isinstance(limitations, list) or not all(isinstance(v, str) and v.strip() for v in limitations):
        raise ValueError("Research limitations must be nonempty strings")
    result["limitations"] = list(dict.fromkeys(v.strip() for v in limitations))
    return result


def validate_research_plan(research, declared_outputs, analysis_unit):
    """Normalize optional research metadata before approval; no I/O or label inference."""
    if research is None:
        return {"enabled": False}
    if not isinstance(research, dict) or not isinstance(research.get("enabled"), bool):
        raise ValueError("Research plan requires an explicit enabled boolean")
    if set(research) - {"enabled", "context", "evidence_outputs"}:
        raise ValueError("Unknown research plan fields")
    if not research["enabled"]:
        if research.get("evidence_outputs"):
            raise ValueError("Disabled research plan cannot declare evidence outputs")
        return {"enabled": False}
    context = _context(research.get("context"), analysis_unit)
    specs = research.get("evidence_outputs")
    if not isinstance(specs, list) or not specs:
        raise ValueError("Enabled research requires explicit evidence_outputs bindings")
    allowed = {item["path"] if isinstance(item, dict) else item for item in declared_outputs}
    normalized, seen = [], set()
    required = {"path", "layer", "omics", "entity_type", "namespace", "columns", "effect_scale", "row_key", "measured_omics", "evidence_context"}
    for spec in specs:
        if not isinstance(spec, dict) or set(spec) != required:
            raise ValueError("Evidence output requires exactly the declared research binding fields")
        item = copy.deepcopy(spec)
        relative = _relative(item["path"])
        if relative not in allowed or relative in seen or Path(relative).suffix.lower() not in {".csv", ".tsv"}:
            raise ValueError("Research output must be a distinct declared CSV/TSV: " + relative)
        seen.add(relative)
        if item["layer"] not in {"de", "pathway_enrichment"}:
            raise ValueError("Unsupported research evidence layer")
        if item["omics"] not in MEASURED_OMICS | {"multiomics", "unknown"}:
            raise ValueError("Unsupported research omics")
        if item["entity_type"] not in {"gene", "protein", "metabolite", "metabolite_feature", "pathway", "region", "variant"}:
            raise ValueError("Unsupported research entity type")
        item["namespace"] = _text(item["namespace"], "namespace")
        columns = item["columns"]
        if (not isinstance(columns, dict) or "entity_id" not in columns or set(columns) - COLUMN_KEYS or
                not all(isinstance(v, str) and v.strip() for v in columns.values()) or
                len(set(columns.values())) != len(columns)):
            raise ValueError("Evidence columns require explicit distinct semantic bindings")
        keys = item["row_key"]
        if (not isinstance(keys, list) or not keys or len(set(keys)) != len(keys) or
                not all(isinstance(v, str) and v.strip() for v in keys)):
            raise ValueError("Evidence row_key requires distinct source column names")
        if item["effect_scale"] not in SCALES:
            raise ValueError("Unknown research effect_scale")
        if item["evidence_context"] not in {"measured_evidence", "derived_estimate", "external_context"}:
            raise ValueError("Invalid research evidence_context")
        measured = item["measured_omics"]
        if (not isinstance(measured, list) or len(set(measured)) != len(measured) or
                set(measured) - MEASURED_OMICS):
            raise ValueError("Invalid measured_omics declaration")
        if item["evidence_context"] != "measured_evidence" and measured:
            raise ValueError("Derived estimates and external context cannot count as measured_omics")
        if item["evidence_context"] == "measured_evidence" and measured != [item["omics"]]:
            raise ValueError("One measured output represents exactly its declared assay modality")
        if item["layer"] == "pathway_enrichment" and (item["entity_type"] != "pathway" or item["evidence_context"] == "measured_evidence"):
            raise ValueError("Pathway enrichment is a derived estimate, not an independent assay")
        if item["layer"] == "de" and "unknown" in context["contrast"].values():
            raise ValueError("DE evidence requires explicitly defined numerator and denominator")
        normalized.append(item)
    return {"enabled": True, "context": context, "evidence_outputs": normalized}


@dataclass
class ResearchContext:
    question: str = "unknown"
    disease: str = "unknown"
    species: str = "unknown"
    tissue: str = "unknown"
    analysis_unit: str = "unknown"
    cohort_id: str = "unknown"
    contrast: dict = field(default_factory=lambda: {"numerator": "unknown", "denominator": "unknown"})
    limitations: list = field(default_factory=list)
    evidence: list = field(default_factory=list)
    artifacts: list = field(default_factory=list)
    task_id: str = "unknown"
    plan_hash: str = "unknown"
    private_ids: list = field(default_factory=list, repr=False)

    def to_dict(self):
        return {"schema_version": "1.0", **asdict(self)}


def _numeric(value, name):
    if value.strip().lower() in MISSING:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid numeric research value for {name}") from exc
    if not math.isfinite(result):
        raise ValueError(f"Nonfinite research value for {name}")
    if name in {"p_value", "adjusted_p"} and not 0 <= result <= 1:
        raise ValueError(f"Research {name} outside [0,1]")
    if name == "n":
        if result < 0 or not result.is_integer():
            raise ValueError("Research n must be a nonnegative integer")
        return int(result)
    return result


def _selected(value):
    if value.strip().lower() in MISSING:
        return None
    if value.strip().lower() in {"true", "1"}:
        return True
    if value.strip().lower() in {"false", "0"}:
        return False
    raise ValueError("Research selected must be an explicitly recorded boolean")


def _direction(effect, scale, layer, status):
    if effect is None or scale == "unknown" or status not in {"tested", "unknown"}:
        return "unknown"
    if scale == "NES":
        return "positive_enrichment" if effect > 0 else "negative_enrichment" if effect < 0 else "zero_estimate"
    null = 1 if scale in {"fold_change", "odds_ratio", "hazard_ratio"} else 0
    if effect == null:
        return "zero_estimate"
    return "higher_in_numerator" if effect > null else "lower_in_numerator"


def _records(content, spec, context, artifact):
    import io
    reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")), delimiter="\t" if spec["path"].endswith(".tsv") else ",")
    columns = spec["columns"]
    if (not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames) or
            not set(columns.values()).union(spec["row_key"]) <= set(reader.fieldnames)):
        raise ValueError("Missing or duplicate bound evidence columns: " + spec["path"])
    records, seen = [], set()
    for row_index, row in enumerate(reader, 1):
        if None in row or any(v is None for v in row.values()):
            raise ValueError("Ragged evidence row: " + spec["path"])
        key = {name: row[name] for name in spec["row_key"]}
        key_tuple = tuple(key.values())
        if key_tuple in seen or any(v.strip().lower() in MISSING for v in key_tuple):
            raise ValueError("Missing or duplicate evidence row key: " + spec["path"])
        seen.add(key_tuple)
        entity = row[columns["entity_id"]].strip()
        if entity.lower() in MISSING:
            raise ValueError("Missing bound evidence entity ID")
        observation = {name: _numeric(row[columns[name]], name) if name in columns else None
                       for name in ("effect", "p_value", "adjusted_p", "n")}
        observation.update({"selected": _selected(row[columns["selected"]]) if "selected" in columns else None,
                            "status": row[columns["status"]].strip() if "status" in columns else "unknown",
                            "effect_scale": spec["effect_scale"], "claim_type": "statistical_fact"})
        observation["direction"] = _direction(observation["effect"], spec["effect_scale"], spec["layer"], observation["status"])
        observation["missing_fields"] = [name for name, column in columns.items() if row[column].strip().lower() in MISSING]
        provenance = {"artifact_path": artifact["path"], "sha256": artifact["sha256"], "row_key": key,
                      "row_index": row_index, "columns": dict(columns), "plan_hash": context.plan_hash}
        identity = {"task_id": context.task_id, "plan_hash": context.plan_hash, "source": artifact,
                    "key": key, "columns": columns, "layer": spec["layer"]}
        record = {"schema_version": "3.0", "evidence_id": "ev_" + _hash_json(identity)[:20],
                  "source": {"omics": spec["omics"], "cohort": context.cohort_id,
                             "evidence_layer": spec["layer"], "evidence_context": spec["evidence_context"],
                             "measured_omics": list(spec["measured_omics"]), "analysis_unit": context.analysis_unit},
                  "biological_entity": {"entity_type": spec["entity_type"], "entity_id": entity,
                                        "namespace": spec["namespace"], "species": context.species,
                                        "symbol": row[columns["symbol"]].strip() if "symbol" in columns else "unknown",
                                        "tissue_context": context.tissue},
                  "observation": observation, "provenance": provenance}
        records.append(record)
    return records


def _validate_context(context):
    validator = jsonschema.Draft7Validator(json.loads((SCHEMAS / "evidence_v3.schema.json").read_text()))
    for record in context.evidence:
        validator.validate(record)
    jsonschema.validate(context.to_dict(), json.loads((SCHEMAS / "research_context.schema.json").read_text()))
    return context


def _private_identifiers(root, plan):
    """Read only explicit metadata bindings; raw IDs stay in the local context."""
    import io
    identifiers, limitations = set(), []
    inputs = {item["path"]: item for item in plan["inputs"]}
    for binding in plan.get("input_bindings", {}).get("matrices", []):
        relative = binding.get("metadata_path")
        if relative not in inputs:
            raise ValueError("Privacy metadata must be a frozen declared input")
        if Path(relative).suffix.lower() not in {".csv", ".tsv"}:
            limitations.append("Private identifiers in bound non-CSV/TSV metadata could not be read; raw context requires local privacy review.")
            continue
        data, _ = _read(root, relative, inputs[relative]["sha256"])
        reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig")), delimiter="\t" if relative.endswith(".tsv") else ",")
        keys = {binding.get("sample_key"), binding.get("pair_key")} - {None, ""}
        if not keys <= set(reader.fieldnames or []):
            raise ValueError("Privacy metadata keys differ from frozen input binding")
        for row in reader:
            if None in row or any(v is None for v in row.values()):
                raise ValueError("Ragged privacy metadata input")
            identifiers.update(row[key].strip() for key in keys if row[key].strip().lower() not in MISSING)
            # Protect complete TCGA barcodes and their participant prefixes.
            for value in row.values():
                for match in re.finditer(r"\bTCGA-[A-Za-z0-9]{2}-[A-Za-z0-9]{4}(?:-[A-Za-z0-9]+)*\b", value):
                    identifiers.update((match.group(), match.group()[:12]))
    return sorted(identifiers), list(dict.fromkeys(limitations))


class TaskResearchAdapter:
    def __init__(self, root, task_id):
        self.root, self.task_id = Path(root).resolve(), task_id

    def load(self):
        from multiomics_tui.task_sessions import TaskStore, canonical_hash
        store = TaskStore(self.root)
        state, plan = store.get(self.task_id), store.plan(self.task_id)
        plan_hash = canonical_hash(plan)
        if state.get("approved_hash") != plan_hash or state.get("status") != "succeeded":
            raise ValueError("Research requires a succeeded task with matching approved plan")
        declared = [p for step in plan["steps"] for p in step["outputs"]]
        research = validate_research_plan(plan.get("research"), declared, plan["analysis_unit"])
        if not research["enabled"]:
            raise ValueError("Task has no approved research bindings; re-plan explicitly")
        base = f"analysis/{self.task_id}"
        artifacts = {}
        def read(relative, expected=None):
            content, artifact = _read(self.root, relative, expected)
            if relative in artifacts and artifacts[relative] != artifact:
                raise ValueError("Research source changed during adapter load")
            artifacts[relative] = artifact
            return content
        plan_path = store.directory(self.task_id) / f"plan_v{state['plan_version']:03d}.json"
        frozen_plan = json.loads(read(plan_path.relative_to(self.root).as_posix()))
        if canonical_hash(frozen_plan) != plan_hash:
            raise ValueError("Approved research plan changed during load")
        result = json.loads(read(f"{base}/result_manifest.json"))
        code = json.loads(read(f"{base}/code_manifest.json"))
        if (result.get("task_id") != self.task_id or result.get("plan_hash") != plan_hash or
                code.get("plan_hash") != plan_hash or code.get("status") != "succeeded"):
            raise ValueError("Research manifests do not match the succeeded approved task")
        if result.get("inputs") != plan["inputs"] or code.get("inputs") != plan["inputs"] or code.get("scripts") != result.get("scripts"):
            raise ValueError("Research manifest input/script bindings differ")
        for item in plan["inputs"]:
            read(item["path"], item["sha256"])
        scripts = code.get("scripts", [])
        if ({s.get("step_id") for s in scripts} != {s["id"] for s in plan["steps"]} or
                len(scripts) != len(plan["steps"]) or not all(s.get("preflight_passed") is True for s in scripts)):
            raise ValueError("Research code manifest lacks completed declared scripts")
        for item in scripts:
            read(f"{base}/{_relative(item['path'])}", item["sha256"])
        outputs = result.get("outputs", [])
        if len(outputs) != len(declared) or {item["path"] for item in outputs} != set(declared):
            raise ValueError("Research result manifest must cover every declared output exactly once")
        cached = {}
        for item in outputs:
            content = read(f"{base}/{_relative(item['path'])}", item["sha256"])
            if len(content) != item.get("bytes"):
                raise ValueError("Research output byte size mismatch")
            cached[item["path"]] = content
        context = ResearchContext(**research["context"], task_id=self.task_id, plan_hash=plan_hash)
        context.private_ids, privacy_limits = _private_identifiers(self.root, plan)
        context.limitations.extend(privacy_limits)
        for spec in research["evidence_outputs"]:
            context.evidence.extend(_records(cached[spec["path"]], spec, context, artifacts[f"{base}/{spec['path']}"]))
        context.artifacts = sorted(artifacts.values(), key=lambda item: item["path"])
        verify_context_sources(self.root, context)
        return _validate_context(context)


def verify_context_sources(root, context):
    """Recheck every source, including outputs not selected for biological interpretation."""
    value = context.to_dict() if isinstance(context, ResearchContext) else context
    artifacts = value.get("artifacts", [])
    if len({a["path"] for a in artifacts}) != len(artifacts):
        raise ValueError("Duplicate research source artifacts")
    by = {a["path"]: a for a in artifacts}
    contents = {}
    for item in artifacts:
        content, _ = _read(root, item["path"], item["sha256"])
        if len(content) != item["bytes"]:
            raise ValueError("Research source byte size mismatch")
        contents[item["path"]] = content
    tables = {}
    for evidence in value.get("evidence", []):
        source = evidence["provenance"]
        if source["artifact_path"] not in by or by[source["artifact_path"]]["sha256"] != source["sha256"]:
            raise ValueError("Research evidence lacks a verified source artifact")
        if source.get("row_index") is None:
            continue  # Legacy records retain their original v2 row-key semantics.
        relative = source["artifact_path"]
        if relative not in tables:
            import io
            tables[relative] = list(csv.DictReader(io.StringIO(contents[relative].decode("utf-8-sig")),
                                                  delimiter="\t" if relative.endswith(".tsv") else ","))
        index = source["row_index"]
        if not isinstance(index, int) or isinstance(index, bool) or not 1 <= index <= len(tables[relative]):
            raise ValueError("Research evidence row_index is unavailable")
        row = tables[relative][index - 1]
        if any(row.get(key) != raw for key, raw in source["row_key"].items()):
            raise ValueError("Research evidence row_key differs from source")
        columns = source["columns"]
        if "entity_id" not in columns or any(name not in row for name in columns.values()):
            raise ValueError("Research evidence columns differ from source")
        if evidence["biological_entity"]["entity_id"] != row[columns["entity_id"]].strip():
            raise ValueError("Research evidence entity differs from source")
        observed = evidence["observation"]
        for name in ("effect", "p_value", "adjusted_p", "n"):
            expected = _numeric(row[columns[name]], name) if name in columns else None
            if observed[name] != expected:
                raise ValueError(f"Research evidence {name} differs from source")
        selected = _selected(row[columns["selected"]]) if "selected" in columns else None
        if observed["selected"] is not selected:
            raise ValueError("Research evidence selection differs from source")
    return True


class LegacyResearchAdapter:
    """Read an existing v2 run without changing it or inventing disease metadata."""
    def __init__(self, root, run, cfg):
        self.root, self.run, self.cfg = Path(root).resolve(), Path(run).resolve(), cfg

    def load(self):
        from .checkers import validate_evidence_registry_v2
        from .reasoning import verify_reasoning_sources
        if not self.run.is_relative_to(self.root):
            raise ValueError("Legacy run must be inside its declared project root")
        base = self.run.relative_to(self.root).as_posix()
        artifacts = {}
        def read(relative):
            content, artifact = _read(self.root, relative)
            artifacts[relative] = artifact
            return content
        path = f"{base}/integration/evidence_registry.v2.jsonl"
        raw = read(path)
        validate_evidence_registry_v2(self.run / "integration/evidence_registry.csv", self.run / "integration/evidence_registry.v2.jsonl", SCHEMAS / "evidence_v2.schema.json")
        read(f"{base}/integration/evidence_registry.csv")
        run_info = json.loads(read(f"{base}/run.json"))
        snapshot = self.run / "reasoning/frozen_sources.json"
        if snapshot.is_file():
            verify_reasoning_sources(self.run, snapshot)
            frozen = json.loads(read(snapshot.relative_to(self.root).as_posix()))
            frozen_hashes = {f"{base}/{item['path']}": item['sha256'] for item in frozen['artifacts']}
        elif (self.run / "integration/evidence_graph.json").is_file():
            from .evidence_graph import load_evidence_graph
            graph = load_evidence_graph(self.root, self.run)
            frozen_hashes = {(item['path'] if item['path'].startswith('knowledge/') else f"{base}/{item['path']}"): item['sha256']
                             for item in graph['source_artifacts']}
            read(f"{base}/integration/evidence_graph.json")
        else:
            raise ValueError("Legacy research requires a preexisting source freeze or verified evidence graph")
        if frozen_hashes.get(path) != artifacts[path]['sha256']:
            raise ValueError("Legacy source freeze does not cover the v2 evidence registry")
        research = self.cfg.get("research", {}).get("context", {})
        context = ResearchContext(**_context(research), task_id=run_info.get("run_id", self.run.name),
                                  plan_hash=run_info.get("fingerprint", "unknown"))
        context.limitations.append("Legacy v2 evidence: only explicitly configured research context is available.")
        for line in raw.decode().splitlines():
            record = json.loads(line)
            provenance = record["provenance"]
            relative = provenance["artifact_path"]
            source_path = relative if relative.startswith("knowledge/") else f"{base}/{relative}"
            read(source_path)
            artifact = artifacts[source_path]
            if frozen_hashes.get(source_path) != artifact['sha256']:
                raise ValueError("Legacy source freeze does not cover the evidence source")
            source = record["source"]
            measured = source["evidence_layer"] == "de" and record["evidence_quality"]["evidence_context"] == "measured_evidence"
            entity, observed = record["biological_entity"], record["observation"]
            context.evidence.append({"schema_version": "3.0", "evidence_id": record["evidence_id"],
                "source": {**source, "evidence_context": record["evidence_quality"]["evidence_context"],
                           "measured_omics": [source["omics"]] if measured else [], "analysis_unit": context.analysis_unit},
                "biological_entity": {**entity, "namespace": "unknown", "species": context.species},
                "observation": {"effect": observed["effect"], "p_value": observed["pvalue"], "adjusted_p": observed["padj"],
                                "n": observed["n_pairs"], "selected": observed["selected"] if isinstance(observed["selected"], bool) else None,
                                "direction": "unknown" if "unknown" in context.contrast.values() else observed["direction"],
                                "effect_scale": observed["effect_scale"], "status": "unknown", "claim_type": "statistical_fact", "missing_fields": []},
                "provenance": {"artifact_path": source_path, "sha256": artifact["sha256"],
                               "row_key": {"legacy_row_key": provenance["row_key"]}, "row_index": None,
                               "columns": {}, "plan_hash": context.plan_hash, "legacy_evidence_id": record["evidence_id"]}})
        context.artifacts = sorted(artifacts.values(), key=lambda item: item["path"])
        verify_context_sources(self.root, context)
        return _validate_context(context)

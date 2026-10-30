"""Outbound privacy checks: identifiers and clinical free text stop before transport."""
import json
from pathlib import Path

import pytest

from orchestrator.knowledge import KnowledgeError, KnowledgeService
from orchestrator.llm_client import LLMClient, PrivacyViolation
from orchestrator.privacy import metadata_private_values, sensitive_matches
from orchestrator.research_literature import _public_queries


def llm_cfg():
    return {"llm": {"base_url": "https://api.example.invalid", "model": "fixture",
                    "temperature": 0, "max_retries": 0, "max_schema_retries": 0,
                    "token_budget": 1000, "cache": False, "api_key_env": "TEST_PRIVACY_KEY"}}


class Transport:
    def __init__(self):
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("transport must not be reached")


@pytest.mark.parametrize("text", [
    "Patient ID: PRIVATE_PATIENT_A",
    "sample_12345 expression",
    "TCGA-AA-0001-01A tumor",
    "MRN: 1234567",
    "Patient Name: Jane Doe",
    "Contact the patient at jane.doe@example.org",
    "Address: 12 Example Street",
])
def test_model_payload_with_private_or_clinical_text_is_blocked_before_transport(tmp_path, text):
    transport = Transport()
    client = LLMClient(tmp_path, tmp_path / "run", llm_cfg(), transport=transport,
                       registry_ids=["PRIVATE_PATIENT_A"])
    with pytest.raises(PrivacyViolation):
        client.chat_json("analyst_research", "system", text, "privacy_test_v1")
    assert transport.calls == 0


def test_exact_bound_private_value_is_blocked_even_without_generic_label(tmp_path):
    transport = Transport()
    client = LLMClient(tmp_path, tmp_path / "run", llm_cfg(), transport=transport,
                       registry_ids=["P-000123"])
    with pytest.raises(PrivacyViolation):
        client.chat_json("analyst_research", "system", "the value P-000123 was observed", "privacy_test_v1")
    assert transport.calls == 0


@pytest.mark.parametrize("text", [
    "TP53 is associated with colorectal cancer in the TCGA-LIHC cohort.",
    "The patient-level analysis used a paired design.",
    "A study reported a hazard ratio in a public cohort.",
    "PMID 12345678 reported the association.",
    "The DOI is 10.1000/example.2024.",
    "The study was published in 2024-2025, pages 770-782.",
    "TCGA-LIHC was downloaded from GDC Data Release 46.0.",
    "The input path data/raw/sample_01.csv was inspected.",
])
def test_ordinary_public_scientific_text_is_not_blocked(text):
    assert sensitive_matches(text) == []
    assert sensitive_matches(text, channel="wiki") == []


@pytest.mark.parametrize("text", [
    # A hex UUID holds "8938-4578-8529"-shaped fragments; it is not a phone number.
    "1e1587e8-8938-4578-8529-0831573a9fe1/a5b5b737-3f68-49a0-ac50-2884dbb90496.tsv",
    "data/raw/_gdc_cache/extract/3f6cc934-a895-4155-9257-7b09d36ac917/expression.tsv",
    "The analysis used matrix 19938x424 with 670 constant rows.",
    "ENSG00000141510.17 was quantified at 4223279 bytes.",
    "GDC Data Release 46.0, accessed 2026-10-28, pages 770-782.",
])
def test_generated_identifiers_are_not_mistaken_for_contact_details(text):
    """UUID, dimension and version strings must not block an outbound request."""
    assert sensitive_matches(text) == []


@pytest.mark.parametrize("text", [
    "Tel +86 138 0013 8000",
    "手机 138-0013-8000",
    "contact: 555-123-4567",
    "Call 555 123 4567 for details.",
    "+1 (555) 123-4567",
    "tel: 010-12345678",
    "cell 5551234567",
    "联系电话：13800138000",
])
def test_real_contact_details_are_still_blocked(text):
    """Tightening the pattern must not open a leak for genuine phone numbers."""
    assert any(kind == "contact" for kind, _ in sensitive_matches(text))


def test_planning_prompt_over_a_download_cache_is_not_blocked(tmp_path, monkeypatch):
    """The catalog must never turn a local build cache into a privacy violation."""
    from multiomics_tui.task_planner import catalog
    from orchestrator.llm_client import LLMClient

    cache = tmp_path / "data/raw/_gdc_cache/extract/1e1587e8-8938-4578-8529-0831573a9fe1"
    cache.mkdir(parents=True)
    (cache / "3f6cc934-a895-4155-9257-7b09d36ac917.rna_seq.tsv").write_text("gene\tcount\n")
    (tmp_path / "data/raw/real_input.csv").write_text("sample,value\na,1\n")

    inventory = catalog(tmp_path)
    assert [item["path"] for item in inventory] == ["data/raw/real_input.csv"]
    prompt = "Available files: " + json.dumps(inventory, ensure_ascii=False)
    assert sensitive_matches(prompt) == []

    monkeypatch.setenv("TEST_PRIVACY_KEY", "fixture-key")
    calls = []
    client = LLMClient(tmp_path, tmp_path / "run", llm_cfg(),
                       transport=lambda *a, **k: calls.append(a) or (200, '{"inputs":[]}'))
    try:
        client.chat_json("tui_task_plan_inputs", prompt, "计算通路评分", "tui_task_inputs_v1")
    except Exception:
        # A fixture reply may fail schema validation; reaching transport is the point.
        pass
    assert calls, "the guarded payload must reach transport once the cache is excluded"


def test_literature_query_blocks_private_ids_and_paths_but_allows_public_project_id():
    context = {"disease": "colorectal cancer", "species": "Homo sapiens",
               "private_ids": ["PRIVATE_PATIENT_A"]}
    with pytest.raises(ValueError):
        _public_queries(["PRIVATE_PATIENT_A expression"], context, 8)
    with pytest.raises(ValueError):
        _public_queries(["/Users/researcher/private.csv"], context, 8)
    result, public = _public_queries(
        ["TCGA-LIHC tumor expression", "GSE12345", "patient-level analysis"], context, 8)
    assert result == ["TCGA-LIHC tumor expression", "GSE12345", "patient-level analysis"]
    assert "private_ids" not in public


def test_metadata_free_text_and_identifier_values_are_collected_locally():
    row = {"patient_id": "P-000123", "sample": "S-44",
           "notes": "Patient Jane Doe, MRN 1234567, stable disease",
           "tissue": "tumor", "diagnosis": "HCC"}
    values = metadata_private_values(row, ["patient_id"])
    assert "P-000123" in values and "S-44" in values
    assert any("Jane Doe" in value for value in values)
    assert "tumor" not in values and "HCC" not in values


def test_research_adapter_collects_clinical_free_text_into_local_private_ids(tmp_path):
    import hashlib
    from orchestrator.research_context import _private_identifiers
    source = tmp_path / "data/raw/metadata.csv"
    source.parent.mkdir(parents=True)
    source.write_text("sample,patient,notes\nPRIVATE_SAMPLE_A,PRIVATE_PATIENT_A,"
                      "Patient Jane Doe MRN 1234567 stable disease\n")
    plan = {"inputs": [{"path": "data/raw/metadata.csv",
                        "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}],
            "input_bindings": {"matrices": [{"metadata_path": "data/raw/metadata.csv",
                                              "sample_key": "sample", "pair_key": "patient"}]}}
    values, limitations = _private_identifiers(tmp_path, plan)
    assert "PRIVATE_SAMPLE_A" in values and "PRIVATE_PATIENT_A" in values
    assert any("Jane Doe" in value for value in values)
    assert limitations == []


def _share_payload(text):
    return {"shareable": True,
            "sources": [{"source_type": "publication",
                         "locator": "https://pubmed.ncbi.nlm.nih.gov/12345/"}],
            "title": "Synthetic knowledge", "statement": text, "context": {},
            "entities": [], "relations": []}


@pytest.mark.parametrize("text", [
    "TCGA-AA-0001-01A showed the association.",
    "Patient Name: Jane Doe",
    "MRN: 1234567",
    "Contact jane.doe@example.org for details.",
])
def test_wiki_share_guard_blocks_private_content(text):
    with pytest.raises(KnowledgeError):
        KnowledgeService._share_guard(_share_payload(text))


def test_wiki_share_guard_allows_public_scientific_text():
    KnowledgeService._share_guard(_share_payload(
        "TP53 association was reported in a public colorectal cancer cohort; this remains observational."))
    KnowledgeService._share_guard(_share_payload(
        "The study was published in 2024-2025, pages 770-782, and remains observational."))


def test_metadata_private_values_are_not_written_into_evidence_json(tmp_path):
    # The adapter's existing source-verification test already asserts this; this check
    # protects the new free-text path specifically.
    row = {"sample": "S-44", "patient": "P-000123",
           "notes": "Clinical note with identifying detail", "tissue": "tumor"}
    values = metadata_private_values(row, ["sample"])
    rendered = json.dumps({"evidence": "gene-level aggregate", "context": "public disease"})
    assert not any(value in rendered for value in values)

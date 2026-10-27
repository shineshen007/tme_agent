"""Attempt-level billing and code correction; synthetic transports only."""
import json

import pytest

from multiomics_tui.model_client import CompatibleClient
from multiomics_tui.model_config import normalize
from multiomics_tui.task_runner import _codegen
from orchestrator.llm_client import LLMBudgetExceeded, LLMTransportError, LLMValidationError


def client(root, transport, **override):
    cfg = {"llm": normalize({"provider": "deepseek", "model": "deepseek-flash",
                             "base_url": "https://api.deepseek.com", "auth_mode": "none",
                             "max_tokens": 32000, "timeout_seconds": 300, "token_budget": 500000,
                             "max_schema_retries": 1, "max_retries": 0, "cache": False, **override})}
    return CompatibleClient(root=root, run=root / "audit", cfg=cfg, transport=transport)


def envelope(parsed=None, *, finish="stop", completion=100, reasoning=30, prompt=10):
    return json.dumps({"choices": [{"finish_reason": finish, "message": {
        "content": json.dumps(parsed or {"ok": True}), "reasoning_content": "PRIVATE_THOUGHT_NEVER_STORE"}}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion,
                  "total_tokens": prompt + completion,
                  "completion_tokens_details": {"reasoning_tokens": reasoning}}})


def record(root):
    return json.loads(next((root / "audit/report/agent/calls").glob("*.json")).read_text())


def test_truncation_correction_accounts_for_both_responses_without_thought_text(tmp_path):
    sent = []
    responses = iter([envelope(finish="length", completion=8000, reasoning=7900, prompt=11),
                      envelope(completion=600, reasoning=300, prompt=12)])

    def transport(url, headers, body, timeout):
        sent.append(json.loads(body))
        assert timeout == 300
        return 200, next(responses)

    model = client(tmp_path, transport)
    model.chat_json("tui_task_codegen", "Return JSON", "Generate current script", "fixture")
    audit = record(tmp_path)
    assert len(sent) == 2 and all(x["max_tokens"] == 32000 and x["thinking"] == {"type": "enabled"} for x in sent)
    correction = sent[1]["messages"][-1]["content"]
    assert "requested schema" in correction and "output contracts" in correction
    assert "evidence ids" not in correction and "no statistics numbers" not in correction
    assert audit["attempt_count"] == 2 and audit["schema_retries"] == 1
    assert [x["finish_reason"] for x in audit["attempts"]] == ["length", "stop"]
    assert [x["reasoning_tokens"] for x in audit["attempts"]] == [7900, 300]
    assert [x["answer_tokens"] for x in audit["attempts"]] == [100, 300]
    assert audit["total_usage"]["total_tokens"] == model.tokens_used == audit["charged_tokens"] == 8623
    assert audit["usage"]["total_tokens"] == 612
    assert "PRIVATE_THOUGHT_NEVER_STORE" not in json.dumps(audit)
    assert all(json.loads(item['response_content']) == {'ok': True} for item in audit['attempts'])
    assert 'shorter' in audit['attempts'][1]['request_messages'][-1]['content'] or 'concise' in audit['attempts'][1]['request_messages'][-1]['content']
    assert all(x["latency_s"] is not None and x["max_tokens"] == 32000 for x in audit["attempts"])


def test_exhausted_truncation_records_all_attempts_and_current_limit(tmp_path):
    model = client(tmp_path, lambda *args: (200, envelope(finish="length")), max_schema_retries=2)
    with pytest.raises(LLMValidationError, match="max_tokens=32000.*thinking=enabled"):
        model.chat_json("tui_task_codegen", "system", "script", "fixture")
    audit = record(tmp_path)
    assert audit["validation"] == "failed" and audit["attempt_count"] == 3
    assert audit["schema_retries"] == 2 and audit["charged_tokens"] == 330
    assert audit["usage"] and audit["total_usage"]["total_tokens"] == 330


@pytest.mark.parametrize("total", [100, 101])
def test_budget_failure_keeps_usage_and_never_sends_another_request(tmp_path, total):
    sent = []

    def transport(*args):
        sent.append(True)
        return 200, envelope(finish="length", completion=total - 10, reasoning=20)

    model = client(tmp_path, transport, token_budget=100)
    with pytest.raises(LLMBudgetExceeded):
        model.chat_json("tui_task_codegen", "system", "script", "fixture")
    audit = record(tmp_path)
    assert len(sent) == 1 and audit["attempt_count"] == 1
    assert audit["validation"] == "failed" and audit["usage"]["total_tokens"] == total
    assert audit["charged_tokens"] == model.tokens_used == total and model.cfg["token_budget"] == 100


def test_http_retries_record_each_attempt_and_no_unreported_token_cost(tmp_path, monkeypatch):
    monkeypatch.setattr("orchestrator.llm_client.time.sleep", lambda _: None)
    responses = iter([(503, "temporary"), (200, envelope())])
    model = client(tmp_path, lambda *args: next(responses), max_retries=1)
    model.chat_json("tui_task_codegen", "system", "script", "fixture")
    audit = record(tmp_path)
    assert [x["http_status"] for x in audit["attempts"]] == [503, 200]
    assert audit["attempts"][0]["usage"] is None
    assert audit["transport_retries"] == 1 and audit["schema_retries"] == 0
    assert model.tokens_used == 110


def test_schema_correction_reserves_remaining_budget_before_another_paid_call(tmp_path):
    sent = []

    def transport(*args):
        sent.append(True)
        return 200, envelope(parsed={'invalid': True}, prompt=19221, completion=10158, reasoning=4894)

    def reject(parsed):
        raise ValueError('A corrected claim/source relation is required')

    model = client(tmp_path, transport, token_budget=50000, max_tokens=16000)
    with pytest.raises(LLMBudgetExceeded, match='schema correction'):
        model.chat_json('hypothesis_reasoner', 'system', 'frozen evidence', 'fixture', validator=reject)
    audit = record(tmp_path)
    assert len(sent) == audit['attempt_count'] == 1
    assert audit['charged_tokens'] == model.tokens_used == 29379
    assert json.loads(audit['attempts'][0]['response_content']) == {'invalid': True}
    assert audit['retry_budget_reservation']['required'] > audit['retry_budget_reservation']['remaining']


def test_failed_transport_is_audited(tmp_path):
    model = client(tmp_path, lambda *args: (400, "invalid request"))
    with pytest.raises(LLMTransportError):
        model.chat_json("tui_task_codegen", "system", "script", "fixture")
    audit = record(tmp_path)
    assert audit["validation"] == "failed" and audit["attempts"][0]["http_status"] == 400
    assert audit["charged_tokens"] == 0 and audit["attempts"][0]["usage"] is None


def test_old_limit_cache_isolated_and_cached_usage_is_not_charged(tmp_path):
    sent = []

    def transport(*args):
        sent.append(True)
        return 200, envelope()

    old = client(tmp_path, transport, max_tokens=8000, cache=True)
    old.chat_json("tui_task_codegen", "system", "script", "fixture")
    new = client(tmp_path, transport, cache=True)
    new.chat_json("tui_task_codegen", "system", "script", "fixture")
    hit = new.chat_json("tui_task_codegen", "system", "script", "fixture")
    assert len(sent) == 2 and hit["cache_hit"] and new.tokens_used == 110
    cached = json.loads(sorted((tmp_path / "audit/report/agent/calls").glob("*.json"))[-1].read_text())
    assert cached["charged_tokens"] == 0 and cached["attempts"] == []


def test_framework_correction_preserves_evidence_rules(tmp_path):
    sent = []
    responses = iter([envelope(finish="length"), envelope()])

    def transport(url, headers, body, timeout):
        sent.append(json.loads(body))
        return 200, next(responses)

    client(tmp_path, transport).chat_json("analyst_transcriptomics", "system", "evidence", "fixture")
    correction = sent[-1]["messages"][-1]["content"]
    assert "no statistics numbers" in correction and "evidence ids" in correction


def test_concurrent_calls_do_not_include_each_others_token_cost(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, Event

    entered = Barrier(2)
    second_finished = Event()

    def transport(url, headers, body, timeout):
        user = json.loads(body)["messages"][-1]["content"]
        entered.wait(timeout=5)
        if user == "first":
            assert second_finished.wait(timeout=5)
            return 200, envelope(completion=120)
        return 200, envelope(completion=100)

    model = client(tmp_path, transport)

    def invoke(user):
        result = model.chat_json("tui_task_codegen", "system", user, "fixture")
        if user == "second":
            second_finished.set()
        return result

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(invoke, "first")
        second = executor.submit(invoke, "second")
        first.result(timeout=10)
        second.result(timeout=10)
    audits = [json.loads(f.read_text()) for f in (tmp_path / "audit/report/agent/calls").glob("*.json")]
    assert len(audits) == 2 and sorted(c["charged_tokens"] for c in audits) == [110, 130]
    assert sum(c["charged_tokens"] for c in audits) == model.tokens_used == 240


def test_six_steps_33_outputs_with_thinking_and_larger_limit(tmp_path):
    plan = {"goal": "Synthetic six-step workflow", "steps": []}
    for number, count in enumerate([4, 8, 7, 5, 3, 6]):
        plan["steps"].append({"id": f"step_{number}", "language": "python", "purpose": "Synthetic check",
                              "outputs": [f"tables/step_{number}_{i}.csv" for i in range(count)]})
    before = json.dumps(plan, sort_keys=True)
    calls = []

    def transport(url, headers, body, timeout):
        payload = json.loads(body)
        context = json.loads(payload["messages"][1]["content"])
        step = context["step"]
        assert payload["max_tokens"] == 32000 and payload["thinking"] == {"type": "enabled"}
        assert len(context["previous_output_contracts"]) == sum(len(x["outputs"]) for x in plan["steps"][:len(calls)])
        calls.append(step["id"])
        raw = {"files": [{"step_id": step["id"], "content": "print('synthetic audit step')\n"}],
               "output_contracts": [{"path": name, "structure": "CSV columns sample_id,value"} for name in step["outputs"]]}
        return 200, envelope(raw)

    code = _codegen(client(tmp_path, transport), plan)
    assert len(code) == len(calls) == 6 and sum(len(x["outputs"]) for x in plan["steps"]) == 33
    assert json.dumps(plan, sort_keys=True) == before

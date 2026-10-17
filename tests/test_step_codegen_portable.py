"""Step generation, bounded correction, checkpoints and execution barrier."""
import copy
import json
import shutil
import sys
from unittest.mock import Mock

import pytest

from multiomics_tui import task_runner
from multiomics_tui.model_client import CompatibleClient
from multiomics_tui.model_config import normalize
from multiomics_tui.task_planner import validate_plan
from multiomics_tui.task_sessions import TaskStore, canonical_hash
from orchestrator.llm_client import LLMBudgetExceeded, LLMValidationError


def raw_plan(n=7):
    return {"goal": "Synthetic seven-step verification", "analysis_unit": "input row",
            "method": "descriptive inventory", "parameters": [], "inputs": ["data/raw/input.csv"],
            "steps": [{"id": f"step_{i:02d}", "language": "python", "purpose": f"Synthetic step {i}",
                       "outputs": [f"tables/step_{i:02d}_{j}.csv" for j in range(4)]} for i in range(n)],
            "limitations": ["Synthetic verification only"]}


def reply(step, content="print('synthetic step')\n"):
    return {"files": [{"step_id": step["id"], "content": content}],
            "output_contracts": [{"path": p, "structure": "CSV columns sample_id,value; one row per synthetic sample"}
                                 for p in step["outputs"]]}


def envelope(parsed, finish="stop"):
    return json.dumps({"choices": [{"message": {"content": json.dumps(parsed)}, "finish_reason": finish}],
                       "usage": {"total_tokens": 8}})


def model(root, transport, **extra):
    cfg = {"llm": normalize({"provider": "ollama", "model": "fixture", "base_url": "http://localhost:1234/v1",
                              "max_tokens": 8000, "cache": False, "max_retries": 0,
                              "max_schema_retries": 1, **extra})}
    return CompatibleClient(root=root, run=root / "audit", cfg=cfg, transport=transport)


def test_seven_steps_28_outputs_use_one_file_and_previous_contracts(tmp_path):
    requests, saved = [], []
    plan = raw_plan()
    frozen = canonical_hash(plan)

    def transport(url, headers, body, timeout):
        payload = json.loads(body)
        context = json.loads(payload["messages"][1]["content"])
        requests.append((payload, context))
        assert "Full frozen plan:" in payload["messages"][0]["content"]
        return 200, envelope(reply(context["step"]))

    code = task_runner._codegen(model(tmp_path, transport), plan,
                                on_step=lambda step, content, contracts: saved.append((step["id"], content, contracts)))
    assert len(code) == len(requests) == len(saved) == 7 and canonical_hash(plan) == frozen
    for i, (payload, context) in enumerate(requests):
        assert payload["max_tokens"] == 8000
        assert context["step"]["id"] == f"step_{i:02d}"
        contracts = context["previous_output_contracts"]
        assert len(contracts) == i * 4
        assert {c["step_id"] for c in contracts} == {f"step_{j:02d}" for j in range(i)}
    records = [json.loads(p.read_text()) for p in (tmp_path / "audit").rglob("*codegen.json")]
    assert len(records) == 7 and all(r["schema"] == "tui_task_codegen_step_v3" for r in records)


def test_truncated_step_corrects_without_regenerating_previous_steps(tmp_path):
    seen = []

    def transport(url, headers, body, timeout):
        step = json.loads(json.loads(body)["messages"][1]["content"])["step"]
        seen.append(step["id"])
        return 200, envelope(reply(step), "length" if len(seen) == 2 else "stop")

    result = task_runner._codegen(model(tmp_path, transport), raw_plan(2))
    assert len(result) == 2 and seen == ["step_00", "step_01", "step_01"]


def test_truncation_exhaustion_identifies_step_and_preserves_checkpoint(tmp_path):
    checkpoints = []

    def transport(url, headers, body, timeout):
        step = json.loads(json.loads(body)["messages"][1]["content"])["step"]
        return 200, envelope(reply(step), "length" if step["id"] == "step_01" else "stop")

    with pytest.raises(LLMValidationError, match="step_01.*truncated"):
        task_runner._codegen(model(tmp_path, transport), raw_plan(2),
                             on_step=lambda step, content, contracts: checkpoints.append(step["id"]))
    assert checkpoints == ["step_00"]


@pytest.mark.parametrize("defect", ["wrong_id", "multiple_files", "short_code", "no_contracts", "wrong_path",
                                   "duplicate_path", "empty_structure"])
def test_schema_defects_correct_before_cache_or_checkpoint(tmp_path, defect):
    seen = []

    def transport(url, headers, body, timeout):
        step = json.loads(json.loads(body)["messages"][1]["content"])["step"]
        seen.append(step["id"])
        result = reply(step)
        if len(seen) == 1:
            if defect == "wrong_id": result["files"][0]["step_id"] = "other_step"
            elif defect == "multiple_files": result["files"].append(copy.deepcopy(result["files"][0]))
            elif defect == "short_code": result["files"][0]["content"] = ""
            elif defect == "no_contracts": result.pop("output_contracts")
            elif defect == "wrong_path": result["output_contracts"][0]["path"] = "tables/undeclared.csv"
            elif defect == "duplicate_path": result["output_contracts"][1] = copy.deepcopy(result["output_contracts"][0])
            else: result["output_contracts"][0]["structure"] = ""
        return 200, envelope(result)

    assert len(task_runner._codegen(model(tmp_path, transport), raw_plan(2))) == 2
    assert seen == ["step_00", "step_00", "step_01"]


def test_budget_not_increased_and_stop_has_step_context(tmp_path):
    calls = []

    def transport(url, headers, body, timeout):
        step = json.loads(json.loads(body)["messages"][1]["content"])["step"]
        calls.append(step["id"])
        return 200, envelope(reply(step))

    client = model(tmp_path, transport, token_budget=10)
    with pytest.raises(LLMBudgetExceeded, match="step_01.*Token budget exceeded"):
        task_runner._codegen(client, raw_plan(3))
    assert calls == ["step_00", "step_01"] and client.cfg["token_budget"] == 10
    assert client.cfg["max_tokens"] == 8000


def approve(root, raw):
    source = root / "data/raw/input.csv"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("sample,value\na,1\nb,2\n")
    store = TaskStore(root)
    state = store.create("Count synthetic input rows")
    plan = validate_plan(root, raw, store.user_text(state["task_id"]), {})
    state = store.save_plan(state["task_id"], plan)
    store.approve(state["task_id"], state["plan_hash"])
    return store, state


@pytest.mark.parametrize("failure", ["generation", "preflight"])
def test_failure_never_executes_and_manifest_preserves_partial_scripts(project_root, monkeypatch, failure):
    raw = raw_plan(2)
    store, state = approve(project_root, raw)
    monkeypatch.setattr(task_runner, "guard_available", lambda: None)
    monkeypatch.setattr(task_runner, "_step_environment", lambda *args: {})
    execute = Mock(side_effect=AssertionError("analysis execution must not start"))
    monkeypatch.setattr(task_runner.subprocess, "run", execute)

    class Client:
        def chat_json(self, role, system, user, schema_name, validator=None):
            step = json.loads(user)["step"]
            if failure == "generation" and step["id"] == "step_01":
                raise LLMValidationError("truncated synthetic reply")
            content = "def invalid(\n" if failure == "preflight" and step["id"] == "step_01" else "print('fixture')\n"
            parsed = reply(step, content)
            validator(parsed)
            return {"parsed": parsed}

    with pytest.raises((LLMValidationError, RuntimeError, ValueError), match="step_01"):
        task_runner.run(project_root, state["task_id"], client=Client())
    execute.assert_not_called()
    directory = project_root / "analysis" / state["task_id"]
    manifest = json.loads((directory / "code_manifest.json").read_text())
    assert manifest["status"] == "failed" and manifest["scripts"][0]["preflight_passed"]
    assert len(manifest["scripts"]) == 1
    assert len(manifest["attempts"]) == (1 if failure == "generation" else 2)
    assert not (directory / "result_manifest.json").exists()
    assert store.get(state["task_id"])["status"] == "failed"


@pytest.mark.skipif(sys.platform != "linux" or shutil.which("Rscript") is None,
                    reason="Integration fixture uses Linux host guard and R")
def test_python_r_python_pipeline_respects_shared_contracts(project_root):
    raw = raw_plan(3)
    raw["steps"][0]["outputs"] = ["tables/intermediate.csv"]
    raw["steps"][1].update(language="r", outputs=["tables/summary.csv"])
    raw["steps"][2]["outputs"] = ["reports/check.txt"]
    scripts = [
        "import csv,os\nfrom pathlib import Path\nsource=Path(os.environ['PROJECT_ROOT'])/'data/raw/input.csv'\n"
        "rows=list(csv.DictReader(source.open()))\npath=Path(os.environ['TASK_DIR'])/'tables/intermediate.csv'\n"
        "with path.open('w',newline='') as stream:\n writer=csv.DictWriter(stream,fieldnames=['sample','value']);writer.writeheader();writer.writerows(rows)\n",
        "d <- read.csv(file.path(Sys.getenv('TASK_DIR'),'tables/intermediate.csv')); "
        "write.csv(data.frame(rows=nrow(d),total=sum(d$value)),file.path(Sys.getenv('TASK_DIR'),'tables/summary.csv'),row.names=FALSE)\n",
        "import csv,os\nfrom pathlib import Path\ntask=Path(os.environ['TASK_DIR'])\n"
        "row=next(csv.DictReader((task/'tables/summary.csv').open()))\nassert row['rows']=='2' and row['total']=='3'\n"
        "(task/'reports/check.txt').write_text('shared schema verified')\n",
    ]
    structures = ["CSV columns sample,value", "CSV columns rows,total", "UTF-8 verification text"]

    class Client:
        def chat_json(self, role, system, user, schema_name, validator=None):
            context = json.loads(user)
            index = int(context["step"]["id"].split("_")[1])
            assert [c["structure"] for c in context["previous_output_contracts"]] == structures[:index]
            parsed = reply(context["step"], scripts[index])
            parsed["output_contracts"][0]["structure"] = structures[index]
            validator(parsed)
            return {"parsed": parsed}

    store, state = approve(project_root, raw)
    assert task_runner.run(project_root, state["task_id"], client=Client()) == 0
    directory = project_root / "analysis" / state["task_id"]
    manifest = json.loads((directory / "code_manifest.json").read_text())
    assert manifest["status"] == "succeeded"
    assert len(manifest["output_contracts"]) == 3 and all(s["preflight_passed"] for s in manifest["scripts"])
    assert store.get(state["task_id"])["status"] == "succeeded"

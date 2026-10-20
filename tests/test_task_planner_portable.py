"""Portable planning regressions; synthetic inputs, no external API or analysis."""
import asyncio
import json
import subprocess
import shutil
from unittest.mock import Mock

import pytest

from multiomics_tui.controller import Session
from multiomics_tui.model_client import CompatibleClient
from multiomics_tui.model_config import normalize
from multiomics_tui.task_planner import PLANNER, generate_plan, validate_plan
from orchestrator.llm_client import LLMConfigError, LLMTransportError, LLMValidationError


REQUEST = (
    "用data/raw中的两个队列的转录组和蛋白组数据，利用R语言GSVA的plage算法计算每个样本在"
    "data/signature/NeuroMetaPathway.rds中8个基因集中的得分，然后取每条通路tumor组的均值减去"
    "normal组的均值得到差值，使用这个差值画圆圈热图，热图颜色表示差值大小，圆圈大小表示-log10FDR\n"
    "使用wilcox，不同cohort分开，每个cohort/组学各一列，总共四列\n"
    "paired signed-rank test\n根据metadata配对信息，使用中文回答\n"
    "来自同一患者，使用配对wilcoxn signed rank\n"
    "1:患者ID是CaseNo列，P表示normal，T表示tumor，只保留配对样本。"
    "2:FDR每一个cohort+组学分别矫正。3:进行基因符号标准化再计算plage评分"
)


def inputs(root):
    for relative in ("data/raw/input.csv", "data/signature/NeuroMetaPathway.rds"):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix == '.rds':
            rscript = shutil.which('Rscript')
            if not rscript:
                pytest.skip('Synthetic RDS planning fixture requires Rscript')
            subprocess.run([rscript,'--vanilla','-e',
                            'saveRDS(list(setA=c("GeneA","GeneB")),commandArgs(TRUE)[1])',str(path)],check=True,capture_output=True)
        else:
            path.write_text("sample,value\nfixture,1\n", encoding="utf-8")


def plan():
    return {
        "goal": "检查中文请求的参数引用", "analysis_unit": "患者", "method": "描述性检查",
        "parameters": [{"name": name, "value": value, "source": "user"} for name, value in (
            ("signature_file", "data/signature/NeuroMetaPathway.rds"),
            ("plage_algorithm", "plage"), ("sample_type_encoding", "P表示normal，T表示tumor"),
            ("patient_id", "CaseNo"), ("pairing", "只保留配对样本"),
            ("test", "paired signed-rank test"), ("sets", "8"),
            ("fdr_scope", "FDR每一个cohort+组学分别矫正"), ("size", "-log10FDR"))],
        "inputs": ["data/raw/input.csv", "data/signature/NeuroMetaPathway.rds"],
        "steps": [{"id": "check_inputs", "language": "python", "purpose": "仅测试规划，不执行分析",
                   "outputs": ["tables/input_audit.csv"]}], "limitations": ["合成测试，未执行组学分析"],
    }


def bad_plan():
    raw = plan()
    for param in raw["parameters"]:
        if param["name"] == "signature_file":
            param["value"] += "（8个基因集）"
        elif param["name"] == "plage_algorithm":
            param["value"] = "GSVA 的 plage 算法"
        elif param["name"] == "sample_type_encoding":
            param["value"] = "P=normal，T=tumor"
    return raw


class Replies:
    def __init__(self, *replies):
        self.replies = iter(replies)
        self.requests = []

    def chat_json(self, role, system, user, schema_name, validator=None):
        assert role in {"tui_task_plan", "tui_task_plan_inputs"}
        self.requests.append((system, user, schema_name))
        if role == "tui_task_plan_inputs":
            self.selected = next(self.replies)
            if isinstance(self.selected, Exception): raise self.selected
            parsed = self.selected if "questions" in self.selected else {"inputs":self.selected["inputs"]}
        else:
            parsed = {**self.selected,"input_bindings":{"inventory":True}}
        validator(parsed)
        return {"parsed": parsed}


def test_screenshot_parameters_use_original_quotes(project_root, stub_cfg):
    inputs(project_root)
    checked = validate_plan(project_root, plan(), REQUEST, stub_cfg)
    assert len(checked["parameters"]) == 9
    assert all(p["value"] in REQUEST for p in checked["parameters"])
    client = Replies(plan())
    generate_plan(client, project_root, REQUEST, stub_cfg, language="zh")
    system, request, schema = client.requests[-1]
    assert "exact, contiguous quotation" in system and "split compound parameters" in PLANNER
    assert request == REQUEST and schema == "tui_task_plan_v5"


def test_all_parameter_source_errors_are_reported(project_root, stub_cfg):
    inputs(project_root)
    with pytest.raises(ValueError) as failure:
        validate_plan(project_root, bad_plan(), REQUEST, stub_cfg)
    for name in ("signature_file", "plage_algorithm", "sample_type_encoding"):
        assert name in str(failure.value)
    assert "Correct ALL" in str(failure.value)


@pytest.mark.parametrize("parameter", [
    {"name": "cutoff", "value": "0.05", "source": "user"},
    {"name": "cutoff", "value": "0.05", "source": "fixed_method"},
    {"name": "cohort", "value": "52", "source": "config:cohort_pairing"},
    {"name": "workers", "value": "3", "source": "config:runtime.workers"},
    {"name": "empty", "value": "", "source": "user"},
])
def test_unprovided_parameters_still_rejected(project_root, stub_cfg, parameter):
    inputs(project_root)
    raw = plan()
    raw["parameters"].append(parameter)
    with pytest.raises(ValueError):
        validate_plan(project_root, raw, REQUEST, stub_cfg)


@pytest.mark.parametrize("path", ["../input.csv", "config/study_config.yaml", "data/raw/missing.csv"])
def test_invalid_inputs_still_rejected(project_root, stub_cfg, path):
    inputs(project_root)
    raw = plan()
    raw["inputs"] = [path]
    with pytest.raises(ValueError):
        validate_plan(project_root, raw, REQUEST, stub_cfg)


def test_actual_client_corrects_all_errors_in_one_retry_and_caches_v5(project_root, stub_cfg):
    inputs(project_root)
    # 复用 stub_cfg 的 runtime（含本机 R 库），规划需要真实执行输入结构探测。
    cfg = {**stub_cfg, "llm": normalize({"provider": "ollama", "base_url": "http://localhost:1234/v1",
                                         "model": "fixture", "max_schema_retries": 1, "max_retries": 0,
                                         "cache": True})}
    bodies = []
    replies = iter(({"inputs":plan()["inputs"]}, {**bad_plan(),"input_bindings":{"inventory":True}}, {**plan(),"input_bindings":{"inventory":True}}))

    def transport(url, headers, body, timeout):
        bodies.append(json.loads(body))
        return 200, json.dumps({"choices": [{"message": {"content": json.dumps(next(replies))},
                                             "finish_reason": "stop"}], "usage": {"total_tokens": 8}})

    client = CompatibleClient(root=project_root, run=project_root / "audit", cfg=cfg, transport=transport)
    checked = generate_plan(client, project_root, REQUEST, cfg, language="zh")
    assert checked["parameters"] == plan()["parameters"] and len(bodies) == 3
    correction = bodies[2]["messages"][-1]["content"]
    for name in ("signature_file", "plage_algorithm", "sample_type_encoding"):
        assert name in correction
    generate_plan(client, project_root, REQUEST, cfg, language="zh")
    assert len(bodies) == 3
    messages = bodies[0]["messages"]
    assert client._cache_key("tui_task_plan", messages, "tui_task_plan_v3") != client._cache_key(
        "tui_task_plan", messages, "tui_task_plan_v5")
    records = [json.loads(p.read_text()) for p in (project_root / "audit").rglob("*task_plan.json")]
    assert records and all(r["schema"] == "tui_task_plan_v5" for r in records)


@pytest.mark.parametrize("error,kind", [
    (LLMValidationError("Parameter signature_file is not in the user request"), "validation"),
    (LLMTransportError("fixture timeout"), "connection"),
    (LLMConfigError("fixture missing model"), "configuration"),
])
def test_failed_task_retries_without_user_text_or_job_changes(project_root, stub_cfg, error, kind):
    inputs(project_root)
    client = Replies(error, plan())
    session = Session(project_root, cfg=stub_cfg, client=client)
    session.jobs.start = Mock()
    response = session.handle_tui(REQUEST)
    task_id = session.active_task
    state = session.tasks.get(task_id)
    assert state["status"] == "blocked" and state["planning_failure_kind"] == kind
    assert not state.get("questions") and state["approved_hash"] is None
    assert "请补充" not in response.message and "/retry" in response.message
    assert str(error) in state["error"] and str(error) not in response.message
    original = session.tasks.user_text(task_id)
    # Restored legacy sessions remain usable, with no metadata migration required.
    del state["planning_failure_kind"]
    session.tasks.save(state)
    restored = Session(project_root, cfg=stub_cfg, client=client)
    restored.jobs.start = Mock()
    response = restored.handle(f"/retry {task_id}")
    assert response.kind == "need_confirm" and restored.active_task == task_id
    assert restored.tasks.user_text(task_id) == original == REQUEST
    assert client.requests[0][1] == client.requests[1][1]
    after = restored.tasks.get(task_id)
    assert after["status"] == "awaiting_approval" and not after["approved_hash"]
    assert after["error"] is None and after["planning_failure_kind"] is None
    session.jobs.start.assert_not_called()
    restored.jobs.start.assert_not_called()
    assert not (project_root / "analysis" / task_id).exists()


@pytest.mark.parametrize("status", ["needs_clarification", "awaiting_approval", "approved", "generating_code",
                                   "running", "verifying", "succeeded", "failed", "killed"])
def test_retry_refuses_non_draft_tasks(project_root, stub_cfg, status):
    session = Session(project_root, cfg=stub_cfg, client=Replies())
    state = session.tasks.create(REQUEST)
    state.update(status=status)
    session.tasks.save(state)
    file = session.tasks.directory(state["task_id"]) / "session.json"
    before = file.read_bytes()
    response = session.handle(f"/retry {state['task_id']}")
    assert response.kind == "text" and file.read_bytes() == before
    assert not session.client.requests


def test_true_clarification_keeps_concrete_questions(project_root, stub_cfg):
    session = Session(project_root, cfg=stub_cfg,
                      client=Replies({"questions": ["请确认 FDR 校正范围。"]}))
    response = session.handle_tui("请分析配对数据")
    state = session.tasks.get(session.active_task)
    assert state["status"] == "needs_clarification" and state["error"] is None
    assert state["questions"] == ["请确认 FDR 校正范围。"]
    assert "请确认 FDR 校正范围" in response.message


def test_retry_preserves_multiple_user_turns_and_invalidates_old_confirmation(project_root, stub_cfg):
    inputs(project_root)
    first, clarification = REQUEST.split("\n", 1)
    client = Replies({"questions": ["请补充配对与校正要求。"]}, plan(),
                     LLMValidationError("fixture source error"), plan())
    session = Session(project_root, cfg=stub_cfg, client=client)
    session.jobs.start = Mock()
    session.handle_tui(first)
    initial = session.handle_tui(clarification)
    task_id = session.active_task
    old_hash = session.tasks.get(task_id)["plan_hash"]
    assert session.tasks.user_text(task_id) == REQUEST
    session.tasks.event(task_id, "user", "Canceled plan confirmation")  # legacy UI record
    session.handle_tui("保持上述要求")
    all_text = session.tasks.user_text(task_id)
    retry = session.handle("/retry")
    assert retry.kind == "need_confirm"
    assert client.requests[-1][1] == all_text == REQUEST + "\n保持上述要求"
    state = session.tasks.get(task_id)
    assert state["plan_version"] == 2 and state["approved_hash"] is None
    # Even if identical content yields the same hash, the old approval token is unusable.
    assert state["plan_hash"] == old_hash
    assert session.confirm(initial.pending_id).kind == "text"
    session.jobs.start.assert_not_called()


def test_retry_empty_and_unknown_tasks_are_nonexecuting(project_root, stub_cfg):
    session = Session(project_root, cfg=stub_cfg, client=Replies())
    assert session.handle("/retry").kind == "text"
    session.handle("/new")
    assert session.handle("/retry").kind == "text"
    assert session.handle("/retry task_20000101_000000_12345678").kind == "text"
    assert session.handle("/retry one two").kind == "text"
    assert not session.client.requests


@pytest.mark.parametrize("size", [(50, 24), (80, 24), (120, 40)])
def test_blocked_ui_details_and_retry_confirmation(project_root, stub_cfg, size):
    pytest.importorskip("textual")
    from textual.widgets import Static
    from multiomics_tui.app import MultiomicsApp
    inputs(project_root)
    session = Session(project_root, cfg=stub_cfg, client=Replies(LLMValidationError("fixture source error"), plan()))
    session.jobs.start = Mock()

    async def scenario():
        app = MultiomicsApp(session)
        async with app.run_test(size=size) as pilot:
            await app._handle_async(REQUEST)
            preview = str(app.query_one("#plan-preview", Static).renderable)
            assert "模型回复校验失败" in preview and "/retry" in preview
            assert "请补充要求" not in preview and "fixture source error" not in preview
            assert "fixture source error" in str(app.query_one("#technical-details", Static).renderable)
            assert not app.query_one("#plan-actions").display
            await app._handle_async("/retry")
            assert session.tasks.get(session.active_task)["status"] == "awaiting_approval"
            assert app.query_one("#plan-actions").display
            session.jobs.start.assert_not_called()

    asyncio.run(scenario())

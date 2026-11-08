"""Portable planning regressions; synthetic inputs, no external API or analysis."""
import asyncio
import copy
import json
import subprocess
import shutil
import ssl
import urllib.error
from http.client import IncompleteRead
from unittest.mock import Mock

import pytest

from multiomics_tui.controller import Session
from multiomics_tui.model_client import CompatibleClient
from multiomics_tui.model_config import normalize
from multiomics_tui.task_planner import PLANNER, generate_plan, validate_plan
from orchestrator.llm_client import LLMConfigError, LLMTransportError, LLMValidationError, PrivacyViolation


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

QUESTIONS = [
    {'id': 'method', 'prompt': '选择差异检验方法', 'mode': 'single', 'options': [
        {'id': 'wilcox', 'label': '配对 Wilcoxon', 'answer': 'paired signed-rank test',
         'reason': '需确认配对关系', 'basis': []},
        {'id': 'paired_t', 'label': '配对 t 检验', 'answer': 'paired t-test',
         'reason': '需检查差值分布', 'basis': []}]},
    {'id': 'outputs', 'prompt': '选择输出', 'mode': 'multiple', 'options': [
        {'id': 'table', 'label': '完整结果表', 'answer': '保留完整结果表', 'reason': '包含阴性结果', 'basis': []},
        {'id': 'plot', 'label': '箱线图', 'answer': '生成boxplot', 'reason': '显示每个样本点', 'basis': []}]},
]


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


@pytest.mark.parametrize("structure", ["normal", "unnamed_empty", "unnamed_nonempty", "duplicate", "nested", "numeric_duplicate"])
def test_screenshot_parameters_use_original_quotes(project_root, stub_cfg, structure):
    inputs(project_root)
    checked = validate_plan(project_root, plan(), REQUEST, stub_cfg)
    assert len(checked["parameters"]) == 9
    assert all(p["value"] in REQUEST for p in checked["parameters"])
    from multiomics_tui.input_inspection import inspect_inputs, public_inspection
    from multiomics_tui.task_runner import file_hash
    fixture = project_root / 'data/raw/input.csv'
    if structure not in {'normal', 'numeric_duplicate'}:
        fixture.write_text({'unnamed_empty': 'sample,group,,\nS1,Tumor,,\nS2,Normal,,\n',
                            'unnamed_nonempty': 'sample,group,\nS1,Tumor,PRIVATE_MARKER\nS2,Normal,x\n',
                            'duplicate': 'sample,group,group\nS1,Tumor,PRIVATE_MARKER\nS2,Normal,x\n',
                            'nested': 'sample,group\nS1,Tumor\nS2,Normal\n'}[structure])
    if structure == 'nested':
        fixture = project_root / 'data/raw/nested.rds'
        subprocess.run([shutil.which('Rscript'), '--vanilla', '-e',
                        'x<-data.frame(sample=c("S1","S2")); x$nested<-list("PRIVATE_MARKER",1:2); saveRDS(x,commandArgs(TRUE)[1])',
                        str(fixture)], check=True, capture_output=True)
        with pytest.raises(ValueError, match='Nested table columns.*file=data/raw/nested.rds column_position=2 type=list') as failure:
            inspect_inputs(project_root, [{'path': 'data/raw/nested.rds', 'sha256': file_hash(fixture)}], stub_cfg)
        assert 'PRIVATE_MARKER' not in str(failure.value)
        return
    if structure == 'numeric_duplicate':
        from multiomics_tui.input_inspection import validate_bindings
        fixture = project_root / 'data/raw/duplicate.rds'
        subprocess.run([shutil.which('Rscript'), '--vanilla', '-e',
                        'x<-matrix(1:4,2,dimnames=list(c("S1","S2"),c("feature","feature")));saveRDS(x,commandArgs(TRUE)[1])',
                        str(fixture)], check=True, capture_output=True)
        local = inspect_inputs(project_root, [{'path':'data/raw/duplicate.rds','sha256':file_hash(fixture)}], stub_cfg)
        assert len(local['profiles'][0]['column_issues']) == 2
        binding = dict(path='data/raw/duplicate.rds', metadata_path='data/raw/duplicate.rds', sample_axis='rows',
                       sample_key='sample', group_key='group', group_map={'Tumor':'tumor','Normal':'normal'},
                       pair_key=None, preprocessing='none')
        with pytest.raises(ValueError, match='column names are ambiguous'):
            validate_bindings({'matrices':[binding]}, local, REQUEST)
        return
    digest = file_hash(fixture)
    local = inspect_inputs(project_root, [{'path': 'data/raw/input.csv', 'sha256': digest}], stub_cfg)
    profile = local['profiles'][0]
    public = public_inspection(local)
    assert file_hash(fixture) == digest
    assert 'PRIVATE_MARKER' not in json.dumps(public) and 'level_hashes' not in json.dumps(public)
    assert 'column:sample' not in json.dumps(public) and 'S1' not in json.dumps(public)
    if structure != 'normal':
        assert profile['dimensions'] == [2, 4 if structure == 'unnamed_empty' else 3]
        assert profile['column_issues'][0]['reason'] == {'unnamed_empty':'unnamed_all_missing',
               'unnamed_nonempty':'unnamed_nonempty','duplicate':'duplicate_name'}[structure]
        assert '' not in profile['columns'] and 'column:' not in profile['keys']
        if structure == 'duplicate':
            assert 'group' not in profile['columns'] and 'column:group' not in profile['keys']
        else:
            assert 'group' in profile['columns']
        assert 'sample' in profile['columns']
    client = Replies(plan())
    generate_plan(client, project_root, REQUEST, stub_cfg, language="zh")
    system, request, schema = client.requests[-1]
    assert "exact, contiguous quotation" in system and "split compound parameters" in PLANNER
    assert request == REQUEST and schema == "tui_task_plan_v8"


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


def test_catalog_hides_build_caches_and_hidden_files(tmp_path):
    """A download cache is not an analyzable input and must not reach the prompt."""
    from multiomics_tui.task_planner import catalog
    cache = tmp_path / "data/raw/_gdc_cache/extract/1e1587e8-8938-4578-8529-0831573a9fe1"
    cache.mkdir(parents=True)
    (cache / "3f6cc934-a895-4155-9257-7b09d36ac917.rna_seq.tsv").write_text("gene\tcount\n")
    (tmp_path / "data/raw/.DS_Store").write_text("\x00")
    (tmp_path / "data/raw/real_input.csv").write_text("sample,value\na,1\n")
    (tmp_path / "data/signature").mkdir(parents=True)
    (tmp_path / "data/signature/sets.gmt").write_text("SET_A\tdesc\tGENE1\tGENE2\n")
    assert [item["path"] for item in catalog(tmp_path)] == [
        "data/raw/real_input.csv", "data/signature/sets.gmt"]


def test_gene_set_resources_are_reported_only_when_inspected():
    from multiomics_tui.task_planner import _gene_set_resources
    assert _gene_set_resources(None) == []
    assert _gene_set_resources({"profiles": [{"path": "data/raw/expr.rds"},
                                             {"path": "data/signature/sets.rds", "gene_sets": {}}]}) \
        == ["data/signature/sets.rds"]


def test_actual_client_corrects_all_errors_in_one_retry_and_caches_v6(project_root, stub_cfg):
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
    assert [p for p in checked["parameters"] if p['name'] != 'planning_protocol'] == plan()["parameters"] and len(bodies) == 3
    correction = bodies[2]["messages"][-1]["content"]
    for name in ("signature_file", "plage_algorithm", "sample_type_encoding"):
        assert name in correction
    generate_plan(client, project_root, REQUEST, cfg, language="zh")
    assert len(bodies) == 3
    messages = bodies[0]["messages"]
    assert client._cache_key("tui_task_plan", messages, "tui_task_plan_v3") != client._cache_key(
        "tui_task_plan", messages, "tui_task_plan_v8")
    records = [json.loads(p.read_text()) for p in (project_root / "audit").rglob("*task_plan.json")]
    assert records and all(r["schema"] == "tui_task_plan_v8" for r in records)


@pytest.mark.parametrize("error,kind", [
    (LLMValidationError("Parameter signature_file is not in the user request"), "validation"),
    (LLMTransportError("fixture timeout"), "connection"),
    (LLMConfigError("fixture missing model"), "configuration"),
    (PrivacyViolation("fixture private identifier"), "privacy"),
    (OSError("fixture inspection startup"), "input_inspection"),
    (subprocess.TimeoutExpired("fixture inspection", 120), "input_inspection"),
    (ValueError("fixture invalid inspection output"), "input_inspection"),
    (IncompleteRead(b"UNSAVED_PARTIAL_RESPONSE"), "connection"),
    (urllib.error.URLError(ssl.SSLEOFError(8, "fixture EOF")), "connection"),
])
def test_failed_task_retries_without_user_text_or_job_changes(project_root, stub_cfg, error, kind, monkeypatch):
    inputs(project_root)
    if kind == 'input_inspection':
        from multiomics_tui.input_inspection import inspect_inputs
        monkeypatch.setattr('multiomics_tui.input_inspection.inspect_inputs', Mock(side_effect=error))
    if isinstance(error, (IncompleteRead, urllib.error.URLError)):
        cfg = {**stub_cfg, "llm": normalize({"provider": "ollama", "base_url": "http://localhost:1234/v1",
                                             "model": "fixture", "max_retries": 1, "cache": False})}
        transport = Mock(side_effect=[error, error])
        monkeypatch.setattr('orchestrator.llm_client.time.sleep', Mock())
        client = CompatibleClient(root=project_root, run=project_root / 'audit', cfg=cfg, transport=transport)
        session_cfg = cfg
    else:
        client = Replies(plan(), plan()) if kind == 'input_inspection' else Replies(error, plan())
        session_cfg = stub_cfg
    session = Session(project_root, cfg=session_cfg, client=client)
    session.jobs.start = Mock()
    response = session.handle_tui(REQUEST)
    task_id = session.active_task
    state = session.tasks.get(task_id)
    assert state["status"] == "blocked" and state["planning_failure_kind"] == kind
    assert not state.get("questions") and state["approved_hash"] is None
    assert "请补充" not in response.message and "/retry" in response.message
    assert str(error) in state["error"] and str(error) not in response.message
    assert state['job_id'] is None
    if isinstance(error, (IncompleteRead, urllib.error.URLError)):
        assert transport.call_count == 2
        assert '响应中断，已尝试 2 次' in response.message
        assert state['planning_transport']['attempt_count'] == 2
        assert 'UNSAVED_PARTIAL_RESPONSE' not in state['error']
        # Keep the same real compatible client for manual retry and return valid plans.
        transport.side_effect = [(200, json.dumps({'choices': [{'message': {'content': json.dumps(reply)},
                                                               'finish_reason': 'stop'}],
                                                  'usage': {'total_tokens': 8}}))
                                 for reply in ({'inputs': plan()['inputs']},
                                               {**plan(), 'input_bindings': {'inventory': True}})]
    if isinstance(error, (IncompleteRead, urllib.error.URLError)):
        session.tasks.event(task_id, 'user', 'Use the requested paired Wilcoxon test.',
                            clarification_selection={'method': ['wilcoxon']})
    if kind == 'input_inspection':
        assert '输入结构检查失败' in response.message
        assert '模型回复校验失败' not in response.message
        assert len(client.requests) == 1
        monkeypatch.setattr('multiomics_tui.input_inspection.inspect_inputs', inspect_inputs)
    original = session.tasks.user_text(task_id)
    # Restored legacy sessions remain usable, with no metadata migration required.
    del state["planning_failure_kind"]
    session.tasks.save(state)
    restored = Session(project_root, cfg=session_cfg, client=client)
    restored.jobs.start = Mock()
    response = restored.handle(f"/retry {task_id}")
    assert response.kind == "need_confirm" and restored.active_task == task_id
    assert restored.tasks.user_text(task_id) == original
    assert original.startswith(REQUEST)
    if isinstance(error, (IncompleteRead, urllib.error.URLError)):
        assert transport.call_count == 4
        assert [json.loads(item.args[2])['messages'][1]['content']
                for item in transport.call_args_list] == [REQUEST, REQUEST, original, original]
        assert original.endswith('Use the requested paired Wilcoxon test.')
    else:
        assert client.requests[0][1] == client.requests[1][1]
    after = restored.tasks.get(task_id)
    assert after["status"] == "awaiting_approval" and not after["approved_hash"]
    assert after["error"] is None and after["planning_failure_kind"] is None
    assert after.get("planning_failure_summary") is None and not after.get("planning_transport")
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


@pytest.mark.parametrize('case', ['legacy', 'structured', 'duplicate_question', 'duplicate_option',
                                 'missing_file', 'missing_column', 'valid_column', 'forged_join', 'bad_mode',
                                 'missing_basis', 'mixed', 'valid_join', 'no_candidates', 'short_ids',
                                 'metadata_path', 'nested_path', 'space_path', 'chinese_path',
                                 'punctuation_paths', 'path_suffix', 'unrelated_basis',
                                 'reason_179', 'reason_180', 'reason_181', 'reason_187',
                                 'reason_1000', 'reason_1001', 'reason_chinese', 'reason_empty',
                                 'reason_blank', 'reason_missing', 'reason_none', 'reason_list',
                                 'reason_number', 'reason_hidden_path', 'prompt_180', 'prompt_181',
                                 'label_80', 'label_81', 'answer_1000', 'answer_1001'])
def test_true_clarification_keeps_concrete_questions(project_root, stub_cfg, case):
    questions = copy.deepcopy(QUESTIONS)
    if case.startswith(('reason_', 'prompt_', 'label_', 'answer_')):
        field, variant = case.split('_', 1)
        target = questions[0] if field == 'prompt' else questions[0]['options'][0]
        limit = {'prompt': 180, 'label': 80, 'answer': 1000, 'reason': 1000}[field]
        value = ('x' * int(variant) if variant.isdigit() else
                 {'chinese': '条件说明。' * 200, 'empty': '', 'blank': ' \n\t ',
                  'missing': None, 'none': None, 'list': [], 'number': 187,
                  'hidden_path': 'x' * 180 + ' 使用 data/raw/invented.csv'}[variant])
        if variant == 'missing':
            target.pop(field)
        else:
            target[field] = value
        frozen = copy.deepcopy(questions)
        if isinstance(value, str) and value.strip() and len(value) <= limit and variant != 'hidden_path':
            assert validate_plan(project_root, {'questions': questions}, REQUEST, stub_cfg)['questions'] == frozen
            assert questions == frozen  # No shortening or normalization of stored content.
        else:
            with pytest.raises(ValueError) as failure:
                validate_plan(project_root, {'questions': questions}, REQUEST, stub_cfg)
            message = str(failure.value)
            assert 'Question method' in message and f'field {field}' in message
            if field != 'prompt':
                assert 'option wilcox' in message
            if variant == 'hidden_path':
                assert 'requires verified basis' in message
            elif not isinstance(value, str):
                assert f'expected string, got {type(value).__name__}; length n/a, limit {limit}' in message
            else:
                assert f'length {len(value)}' in message and f'limit {limit}' in message
                assert ('empty or whitespace-only' if not value.strip() else 'exceeds') in message
        return
    if case == 'legacy':
        questions = ['请确认 FDR 校正范围。']
    elif case == 'duplicate_question':
        questions[1]['id'] = questions[0]['id']
    elif case == 'duplicate_option':
        questions[0]['options'][1]['id'] = questions[0]['options'][0]['id']
    elif case in {'missing_file', 'missing_column', 'valid_column'}:
        inputs(project_root)
        questions[0]['options'][0]['reason'] += ' 需要核实依据。' * 30
        questions[0]['options'][0]['basis'] = [{'path': 'data/raw/missing.csv' if case == 'missing_file' else 'data/raw/input.csv',
                                               'field': 'sample' if case == 'valid_column' else 'invented_field'}]
    elif case == 'forged_join':
        questions[0]['options'][0]['reason'] += ' 需要核实依据。' * 30
        questions[0]['options'][0]['basis'] = [{'matrix': 'data/raw/input.csv', 'matched': 99}]
    elif case == 'bad_mode':
        questions[0]['mode'] = 'guess'
    elif case == 'missing_basis':
        questions[0]['options'][0]['answer'] = '使用 data/raw/invented.csv'
    elif case == 'mixed':
        questions[1] = 'legacy question'
    elif case == 'no_candidates':
        questions[0]['options'] = []
    elif case == 'short_ids':
        questions[0]['id'] = 'a'
        questions[0]['options'][0]['id'] = 'b'
    elif case in {'metadata_path', 'nested_path', 'space_path', 'chinese_path',
                  'punctuation_paths', 'path_suffix', 'unrelated_basis'}:
        relative = {'metadata_path': 'data/metadata/Clinic_109.rds',
                    'nested_path': 'data/raw/metadata/analysis/workspace/input.csv',
                    'space_path': 'data/metadata/Clinical sample table.csv'}.get(case, 'data/metadata/Clinic_109.rds')
        path = project_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('sample,group\nfixture,tumor\n', encoding='utf-8')
        options = questions[0]['options']
        options[0]['basis'] = [{'path': relative}]
        options[0]['answer'] = 'Use ' + relative + '; require exact sample matching.'
        if case == 'chinese_path':
            options[0]['answer'] = '使用' + relative + '，确认样本分组。'
        elif case == 'punctuation_paths':
            options[0]['label'] = '`' + relative + '`'
            options[0]['answer'] = '(' + relative + '); 再核对“' + relative + '”。'
            options[0]['reason'] = '[' + relative + ']，已列于文件清单。'
        elif case == 'path_suffix':
            options[0]['answer'] = relative + '.missing'
        elif case == 'unrelated_basis':
            options[0]['answer'] = 'Use data/metadata/invented.csv'
        if case in {'path_suffix', 'unrelated_basis'}:
            with pytest.raises(ValueError, match='Question method, option wilcox, field answer'):
                validate_plan(project_root, {'questions': questions}, REQUEST, stub_cfg)
        else:
            assert validate_plan(project_root, {'questions': questions}, REQUEST, stub_cfg)['questions'] == questions
        return
    inspection = {'profiles': [{'path': 'data/raw/input.csv', 'columns': {'sample': {'type': 'character', 'missing': 0,
                                                                                'unique': 1, 'known_levels': []}}, 'keys': {}}],
                  'software': {}, 'apis': {}}
    if case == 'valid_join':
        from multiomics_tui.input_inspection import public_inspection
        inspection['profiles'][0]['keys'] = {'column:sample': {'values': ['synthetic_hash']}}
        inspection['profiles'].append({'path': 'data/raw/matrix.rds', 'numeric_matrix': True,
                                       'columns': {}, 'keys': {'columns': {'values': ['synthetic_hash'],
                                                                          'missing': 0, 'duplicates': 0}}})
        questions[0]['options'][0]['basis'] = public_inspection(inspection)['joins']
        assert questions[0]['options'][0]['basis']
    if case not in {'legacy', 'structured', 'valid_column', 'valid_join', 'no_candidates', 'short_ids'}:
        with pytest.raises(ValueError) as failure:
            validate_plan(project_root, {'questions': questions}, REQUEST, stub_cfg, inspection=inspection)
        if case in {'missing_file', 'missing_column', 'forged_join', 'missing_basis'}:
            assert 'Question method, option wilcox, field ' in str(failure.value)
        return
    if case in {'valid_column', 'valid_join', 'no_candidates', 'short_ids'}:
        assert validate_plan(project_root, {'questions': questions}, REQUEST, stub_cfg, inspection=inspection)['questions'] == questions
        return
    if case == 'legacy':
        assert validate_plan(project_root, {'questions': questions}, REQUEST, stub_cfg)['questions'] == questions
        session = Session(project_root, cfg=stub_cfg, client=Replies())
        state = session.tasks.create(REQUEST)
        state.update(status='needs_clarification', questions=questions)
        session.tasks.save(state)
        assert session.tasks.get(state['task_id'])['questions'] == questions
        with pytest.raises(ValueError, match='structured'):
            generate_plan(Replies({'questions': questions}), project_root, REQUEST, stub_cfg)
        return
    inputs(project_root)
    client = Replies({'questions': questions}, {'questions': questions}, plan())
    session = Session(project_root, cfg=stub_cfg,
                      client=client)
    response = session.handle_tui(REQUEST)
    state = session.tasks.get(session.active_task)
    assert state["status"] == "needs_clarification" and state["error"] is None
    assert state['questions'] == questions
    task_id, digest = state['task_id'], state['clarification_hash']
    original = session.tasks.user_text(task_id)
    answers = {'method': {'selected': ['wilcox'], 'custom': ''},
               'outputs': {'selected': ['table', 'plot'], 'custom': ''}}
    session.revise_plan('', task_id, expected_clarification_hash=digest, regenerate_options=True)
    new_digest = session.tasks.get(task_id)['clarification_hash']
    assert new_digest != digest and session.tasks.user_text(task_id) == original
    initial_calls = len(client.requests)
    session.revise_plan('', task_id, clarification_answers=answers, expected_clarification_hash=digest)
    assert len(client.requests) == initial_calls
    digest = new_digest
    for invalid in ({}, {'method': {'selected': ['invented']}},
                    {**answers, 'method': {'selected': ['wilcox', 'paired_t']}},
                    {**answers, 'method': {'selected': ['unknown']}},
                    {**answers, 'method': {'selected': ['custom'], 'custom': ''}}):
        rejected = session.revise_plan('', task_id, clarification_answers=invalid, expected_clarification_hash=digest)
        assert rejected.kind == 'text' and session.tasks.user_text(task_id) == original
        assert len(client.requests) == initial_calls
    session.revise_plan('', task_id, clarification_answers=answers, expected_clarification_hash='stale')
    assert len(client.requests) == initial_calls
    other = session.tasks.create('another task')
    session.active_task = other['task_id']
    session.revise_plan('', task_id, clarification_answers=answers, expected_clarification_hash=digest)
    assert len(client.requests) == initial_calls
    session.active_task = task_id
    response = session.revise_plan('', task_id, clarification_answers=answers, expected_clarification_hash=digest)
    assert response.kind == 'need_confirm'
    assert 'paired signed-rank test' in session.tasks.user_text(task_id)
    assert '保留完整结果表' in session.tasks.user_text(task_id)
    assert QUESTIONS[0]['prompt'] not in session.tasks.user_text(task_id)
    assert session.tasks.get(task_id)['approved_hash'] is None
    count = len(client.requests)
    session.revise_plan('', task_id, clarification_answers=answers, expected_clarification_hash=digest)
    assert len(client.requests) == count


def test_retry_preserves_multiple_user_turns_and_invalidates_old_confirmation(project_root, stub_cfg):
    inputs(project_root)
    first, clarification = REQUEST.split("\n", 1)
    client = Replies({"questions": QUESTIONS}, plan(),
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
    # The new user request changes the frozen knowledge query, so approval binds
    # a new plan even when the statistical steps remain the same.
    assert state["plan_hash"] != old_hash
    assert [{k: v for k, v in s.items() if k != 'method_ids'} for s in session.tasks.plan(task_id)['steps']] == plan()['steps']
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
@pytest.mark.parametrize("failure_kind", ['validation', 'input_inspection'])
def test_blocked_ui_details_and_retry_confirmation(project_root, stub_cfg, size, failure_kind):
    pytest.importorskip("textual")
    from textual.widgets import Static
    from multiomics_tui.app import MultiomicsApp
    inputs(project_root)
    failure = LLMValidationError("fixture source error")
    if failure_kind == 'input_inspection':
        failure = ValueError("fixture source error")
        failure.planning_failure_kind = failure_kind
    session = Session(project_root, cfg=stub_cfg, client=Replies(failure, plan()))
    session.jobs.start = Mock()

    async def scenario():
        app = MultiomicsApp(session)
        async with app.run_test(size=size) as pilot:
            await app._handle_async(REQUEST)
            preview = str(app.query_one("#plan-preview", Static).renderable)
            expected = '输入结构检查失败' if failure_kind == 'input_inspection' else '模型回复校验失败'
            assert expected in preview
            if failure_kind == 'validation':
                assert '/retry' in preview
            assert "请补充要求" not in preview and "fixture source error" not in preview
            assert "fixture source error" in str(app.query_one("#technical-details", Static).renderable)
            assert not app.query_one("#plan-actions").display
            await app._handle_async("/retry")
            assert session.tasks.get(session.active_task)["status"] == "awaiting_approval"
            assert app.query_one("#plan-actions").display
            session.jobs.start.assert_not_called()

    asyncio.run(scenario())

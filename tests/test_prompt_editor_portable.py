"""Request input integrity: synthetic text, no external model or scientific run."""
import asyncio
import copy
import json
import time
from unittest.mock import DEFAULT, Mock

import pytest
from textual.events import Key, Paste
from textual.widgets import Button, Input, RadioButton, RadioSet, SelectionList, Static
from textual._xterm_parser import XTermParser

from multiomics_tui.app import MultiomicsApp
from multiomics_tui.controller import Session
from multiomics_tui.model_client import CompatibleClient
from multiomics_tui.model_config import normalize
from multiomics_tui.prompt_editor import PromptEditor
from multiomics_tui.task_planner import generate_plan
from test_task_planner_portable import QUESTIONS


async def settle(pilot, session, app, *, condition=None, timeout=60.0):
    """等待后台规划线程完成。

    规划在 asyncio.to_thread 中执行，并会真实启动 R 输入探测，耗时随机器变化；
    按 pause() 次数计时会误判，因此按墙钟时间轮询。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await pilot.pause()
        await asyncio.sleep(0.02)
        if not app._busy and session.active_task is not None and (
                condition is None or condition()):
            return
    raise AssertionError("后台规划未在超时前完成")


TAIL = "末尾要求：使用配对检验，保留阴性结果"
LONG = "  中文说明  tumor normal\n" + "完整要求  含 空格\n" * 900 + TAIL + "\n  "


def reply():
    return {"goal": "合成输入完整性检查", "analysis_unit": "输入行", "method": "读取",
            "parameters": [{"name": "tail", "value": TAIL, "source": "user"}],
            "inputs": ["data/raw/input.csv"],
            "steps": [{"id": "inspect", "language": "python", "purpose": "检查合成文件",
                       "outputs": ["tables/check.csv"]}], "limitations": []}


class Capture:
    def __init__(self):
        self.requests = []

    def chat_json(self, role, system, user, schema, validator=None):
        self.requests.append((system, user, schema))
        raw = {"inputs":reply()["inputs"]} if role=="tui_task_plan_inputs" else {**reply(),"input_bindings":{"inventory":True}}
        validator(raw)
        return {"parsed": raw}


def setup_session(root, cfg):
    path = root / "data/raw/input.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("sample,value\nfixture,1\n")
    client = Capture()
    session = Session(root, cfg=cfg, client=client)
    session.jobs.start = Mock()
    return session, client


@pytest.mark.parametrize("text", [
    "first second  third", "中文 全角　空格\u00a0尾部", "data/raw/path with spaces.csv",
    "first line\n第二行 tumor normal\nlast line", "first\r\n第二行\r\nlast", LONG,
], ids=["spaces", "unicode_spaces", "path_spaces", "multiline", "windows_lines", "long"])
def test_bracketed_paste_inserts_full_text_once_without_submission(project_root, stub_cfg, text):
    async def scenario():
        session, client = setup_session(project_root, stub_cfg)
        app = MultiomicsApp(session)
        async with app.run_test(size=(50, 24)) as pilot:
            editor = app.query_one("#chat-input", PromptEditor)
            events = list(XTermParser(lambda: False).feed("\x1b[200~" + text + "\x1b[201~"))
            assert len(events) == 1 and isinstance(events[0], Paste)
            # Input enters through App, matching a terminal driver, not a widget shortcut.
            app.post_message(events[0])
            await pilot.pause()
            assert editor.text == text.replace("\r\n", "\n")
            assert not client.requests and session.active_task is None
            assert 2 <= editor.region.height <= 4
            assert app.query_one("#input-card").region.bottom <= 24
            await pilot.resize_terminal(80, 40)
            await pilot.pause()
            assert editor.text == text.replace("\r\n", "\n")
            assert 2 <= editor.region.height <= 6
            session.jobs.start.assert_not_called()
    asyncio.run(scenario())


@pytest.mark.parametrize("send", ["enter", "button", "wizard", "wizard_legacy", "wizard_long"])
def test_full_long_prompt_reaches_session_and_model_once(project_root, stub_cfg, send):
    async def scenario():
        session, client = setup_session(project_root, stub_cfg)
        app = MultiomicsApp(session)
        if send.startswith('wizard'):
            questions = copy.deepcopy(QUESTIONS)
            if send == 'wizard_long':
                questions[0]['options'][0]['reason'] = '详细条件。' * 36 + '单选完整说明尾部，需核实映射才能分析。'
                questions[1]['options'][0]['reason'] = '输出条件。' * 36 + '多选完整说明尾部，保留全部阴性结果。'
            replies = [{'parsed': {'questions': questions}}, DEFAULT, DEFAULT]
            if send == 'wizard_legacy':
                legacy = session.tasks.create(TAIL)
                legacy.update(status='needs_clarification', questions=['历史长问题，需要用户说明具体条件。'])
                session.tasks.save(legacy)
            client.chat_json = Mock(wraps=client.chat_json, side_effect=replies)
            async with app.run_test(size=(80, 24)) as pilot:
                await app._handle_async('/session ' + legacy['task_id'] if send == 'wizard_legacy' else TAIL)
                if send == 'wizard_legacy':
                    assert app.query_one('#clarification-regenerate').display
                    app.query_one('#clarification-regenerate', Button).press()
                    await settle(pilot, session, app)
                initial_calls = client.chat_json.call_count
                task_id = session.active_task
                question_hash = session.tasks.get(task_id)['clarification_hash']
                assert app.query_one('#clarification-wizard').display
                assert not app.query_one('#plan-preview').display
                assert not any(b.value for b in app.query('#clarification-single RadioButton'))
                app.query_one('#clarification-next', Button).press()
                await pilot.pause()
                assert session.tasks.get(task_id).get('clarification_step', 0) == 0
                choice = app.query_one('#clarification-choice-0', RadioButton)
                if send == 'wizard_long':
                    assert questions[0]['options'][0]['reason'][:180] in choice.label.plain
                    assert '完整说明见 Details' in choice.label.plain
                    assert '单选完整说明尾部' not in choice.label.plain
                    assert questions[0]['options'][0]['reason'] in str(app.query_one('#technical-details', Static).renderable)
                choice.scroll_visible(animate=False, immediate=True)
                await pilot.pause()
                await pilot.click('#clarification-choice-0')
                assert choice.value
                await pilot.resize_terminal(50, 24)
                app._refresh_task_view()
                await pilot.pause()
                assert choice.value and client.chat_json.call_count == initial_calls
                assert session.tasks.get(task_id)['clarification_hash'] == question_hash
                assert session.tasks.get(task_id)['questions'] == questions
                app.save_screenshot(filename=f'{send}_narrow.svg', path=str(project_root))
                # Keyboard selection of the custom choice, then persistence.
                app.query_one('#clarification-single', RadioSet).focus()
                await pilot.press('down', 'down', 'space')
                assert app.query_one('#clarification-choice-2', RadioButton).value
                app.query_one('#clarification-custom', Input).value = '使用自定义检验说明'
                app.query_one('#clarification-next', Button).press()
                await pilot.pause()
                assert session.tasks.get(task_id)['clarification_step'] == 1
                old_click = Button.Pressed(app.query_one('#clarification-back', Button))
                assert app.query_one('#clarification-multiple').display
                if send == 'wizard_long':
                    preview = app.query_one('#clarification-multiple', SelectionList).get_option_at_index(0).prompt.plain
                    assert questions[1]['options'][0]['reason'][:180] in preview
                    assert '完整说明见 Details' in preview and '多选完整说明尾部' not in preview
                    assert questions[1]['options'][0]['reason'] in str(app.query_one('#technical-details', Static).renderable)
                assert not app.query_one('#clarification-multiple', SelectionList).selected
                app.query_one('#clarification-multiple', SelectionList).select('unknown')
                app.query_one('#clarification-next', Button).press()
                await pilot.pause()
                await app.on_button_pressed(old_click)
                assert session.tasks.get(task_id)['clarification_step'] == 2
                app.query_one('#clarification-submit', Button).press()
                await pilot.pause()
                assert session.tasks.get(task_id)['status'] == 'needs_clarification'
                assert client.chat_json.call_count == initial_calls
                app.query_one('#clarification-back', Button).press()
                await pilot.pause()
                app.query_one('#clarification-multiple', SelectionList).deselect_all().select('table').select('plot')
                app._refresh_task_view()
                assert session.tasks.user_text(task_id) == TAIL
                session.jobs.start.assert_not_called()
            # A new app restores the draft only after selecting the saved task.
            restored = MultiomicsApp(session)
            async with restored.run_test(size=(50, 24)) as pilot:
                await restored._handle_async('/session ' + task_id)
                await pilot.pause()
                assert set(restored.query_one('#clarification-multiple', SelectionList).selected) == {'table', 'plot'}
                assert session.tasks.get(task_id)['clarification_hash'] == question_hash
                assert session.tasks.get(task_id)['questions'] == questions
                restored.query_one('#clarification-back', Button).press()
                await pilot.pause()
                assert restored.query_one('#clarification-custom', Input).value == '使用自定义检验说明'
                assert restored.query_one('#clarification-choice-2', RadioButton).value
                # Changing a restored choice still preserves radio exclusivity.
                restored.query_one('#clarification-choice-0', RadioButton).value = True
                await pilot.pause()
                assert not restored.query_one('#clarification-choice-2', RadioButton).value
                if send != 'wizard_long':
                    restored.query_one('#clarification-choice-2', RadioButton).value = True
                await pilot.pause()
                restored.query_one('#clarification-next', Button).press()
                await pilot.pause()
                restored.query_one('#clarification-next', Button).press()
                await pilot.pause()
                restored.query_one('#clarification-submit', Button).press()
                restored.query_one('#clarification-submit', Button).press()
                await settle(pilot, session, restored)
                assert session.tasks.get(task_id)['status'] == 'awaiting_approval'
                assert session.tasks.get(task_id)['approved_hash'] is None
                assert client.chat_json.call_count == initial_calls + 2
                if send == 'wizard_long':
                    events = [json.loads(line) for line in (session.tasks.directory(task_id) / 'events.jsonl').read_text().splitlines()]
                    submitted = [event for event in events if event.get('clarification_hash')][-1]
                    assert submitted['clarification_hash'] == question_hash
                    assert submitted['clarification_selection']['method'] == ['wilcox']
                    assert questions[0]['options'][0]['answer'] in submitted['content']
                    assert '说明尾部' not in submitted['content'] and '完整说明见 Details' not in submitted['content']
                else:
                    assert '使用自定义检验说明' in session.tasks.user_text(task_id)
                session.jobs.start.assert_not_called()
            return
        async with app.run_test(size=(80, 24)) as pilot:
            app.post_message(Paste(LONG))
            await pilot.pause()
            editor = app.query_one("#chat-input", PromptEditor)
            if send == "enter":
                await pilot.press("enter", "enter")
            else:
                app.query_one("#send-prompt", Button).press()
            await settle(pilot, session, app)
            assert len(client.requests) == 2
            system, user, schema = client.requests[-1]
            assert user == LONG and LONG in system and schema == "tui_task_plan_v7"
            assert session.tasks.user_text(session.active_task) == LONG
            assert session.tasks.get(session.active_task)["status"] == "awaiting_approval"
            assert editor.text == ""
            session.jobs.start.assert_not_called()
            assert not (project_root / "analysis" / session.active_task).exists()
    asyncio.run(scenario())


def test_manual_newline_selection_paste_and_command_completion(project_root, stub_cfg):
    from textual.widgets.text_area import Selection

    async def scenario():
        session, client = setup_session(project_root, stub_cfg)
        app = MultiomicsApp(session)
        async with app.run_test() as pilot:
            editor = app.query_one("#chat-input", PromptEditor)
            editor.value = "first line"
            await pilot.press("ctrl+j", "x")
            assert editor.text == "first line\nx" and not client.requests
            editor.selection = Selection((0, 0), (1, 1))
            app.post_message(Paste("替换  空格\n完整文本"))
            await pilot.pause()
            assert editor.text == "替换  空格\n完整文本"
            editor.value = "/retr"
            await pilot.press("right")
            assert editor.text == "/retry" and not client.requests
            editor.value = "/sta"
            await pilot.press("tab")
            assert editor.text == "/status" and not client.requests
            app._set_busy(True)
            editor.value = "/retr"
            await pilot.press("right")
            assert editor.text == "/retr"  # Mutating suggestions remain unavailable while busy.
    asyncio.run(scenario())


def test_busy_submission_preserves_exact_multiline_draft(project_root, stub_cfg):
    async def scenario():
        session, client = setup_session(project_root, stub_cfg)
        app = MultiomicsApp(session)
        async with app.run_test() as pilot:
            editor = app.query_one("#chat-input", PromptEditor)
            app._set_busy(True)
            app.post_message(Paste(LONG))
            await pilot.pause()
            await pilot.press("enter")
            app.query_one("#send-prompt", Button).press()
            await pilot.pause()
            assert editor.text == LONG and not client.requests and session.active_task is None
            session.jobs.start.assert_not_called()
    asyncio.run(scenario())


def test_enter_freezes_request_before_next_queued_character(project_root, stub_cfg):
    async def scenario():
        session, client = setup_session(project_root, stub_cfg)
        app = MultiomicsApp(session)
        async with app.run_test() as pilot:
            editor = app.query_one("#chat-input", PromptEditor)
            editor.value = TAIL
            app.post_message(Key("enter", "\r"))
            app.post_message(Key("x", "x"))
            await settle(pilot, session, app, condition=lambda: bool(client.requests))
            assert len(client.requests) == 2 and client.requests[0][1] == TAIL
            assert editor.text == "x"
            assert session.tasks.user_text(session.active_task) == TAIL
            session.jobs.start.assert_not_called()
    asyncio.run(scenario())


def test_context_overflow_preserves_full_request_and_is_not_clarification(project_root, stub_cfg):
    session, _ = setup_session(project_root, stub_cfg)
    cfg = {"llm": normalize({"provider": "ollama", "base_url": "http://localhost:1234/v1",
                              "model": "fixture", "max_retries": 0, "cache": False})}
    sent = []

    def transport(url, headers, body, timeout):
        sent.append(json.loads(body))
        return 400, '{"error":{"code":"context_length_exceeded","message":"maximum context length exceeded"}}'

    session.client = CompatibleClient(root=project_root, run=project_root / "audit", cfg=cfg,
                                      transport=transport)
    response = session.handle_tui(LONG)
    state = session.tasks.get(session.active_task)
    assert len(sent) == 1 and sent[0]["messages"][-1]["content"] == LONG
    assert session.tasks.user_text(session.active_task) == LONG
    assert state["status"] == "blocked" and state["planning_failure_kind"] == "request_size"
    assert "上下文" in response.message and "原文已保留" in response.message
    assert not state["questions"] and state["approved_hash"] is None
    session.jobs.start.assert_not_called()


def test_old_plan_hash_and_session_remain_readable(project_root, stub_cfg):
    session, client = setup_session(project_root, stub_cfg)
    state = session.tasks.create(TAIL)
    plan = generate_plan(client, project_root, TAIL, stub_cfg)
    saved = session.tasks.save_plan(state["task_id"], plan)
    before = session.tasks.directory(state["task_id"]) / "session.json"
    old_bytes = before.read_bytes()
    restored = Session(project_root, cfg=stub_cfg, client=client)
    restored.handle("/session " + state["task_id"])
    assert before.read_bytes() == old_bytes
    assert restored.tasks.get(state["task_id"])["plan_hash"] == saved["plan_hash"]
    assert restored.tasks.user_text(state["task_id"]) == TAIL

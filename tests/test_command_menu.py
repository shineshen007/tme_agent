"""Slash-command completion menu and the /new return-to-home contract.

Synthetic session only: no model request and no analysis job is started.
"""
import asyncio
import re

import pytest

from multiomics_tui.app import MultiomicsApp
from multiomics_tui.command_menu import COMMANDS, COMMAND_LIST, completion, matches
from multiomics_tui.controller import READONLY_COMMANDS, Session
from multiomics_tui.prompt_editor import PromptEditor


@pytest.fixture
def session(project_root, stub_cfg):
    return Session(project_root, cfg=stub_cfg, client=None)


def menu_rows(app):
    return [child.renderable.plain for child in app._menu.children]


def menu_names(app):
    """Command names only: each row is marker, padded name, hint."""
    names = []
    for row in menu_rows(app):
        field = re.split(r"\s{2,}", row.strip())[0]
        names.append(field.split(" ")[-1])
    return names


def test_catalogue_covers_every_dispatchable_command():
    """A command must not exist in one place (help, completion) and not the other."""
    from multiomics_tui.controller import COMMAND_INTENTS
    names = set(COMMAND_LIST)
    assert set(READONLY_COMMANDS) <= names
    assert set(COMMAND_INTENTS) <= names
    assert {"/new", "/cd", "/session", "/sessions", "/key", "/model", "/retry",
            "/approve", "/cancel", "/quit", "/kill", "/ask"} <= names
    assert len(names) == len(COMMANDS)


@pytest.mark.parametrize("typed,expected", [
    ("/", ["/help", "/status", "/runs", "/figures", "/report", "/jobs"]),
    ("/ret", ["/retry"]),
    ("/rep", ["/report"]),
    ("/re", ["/report", "/retry", "/resume"]),
    ("/ver", ["/verify", "/verify-dossiers"]),
])
def test_prefix_matching_is_ordered_and_capped(typed, expected):
    assert [item.name for item in matches(typed)] == expected


@pytest.mark.parametrize("typed", [
    "", "分析这批数据", "/retry ", "/new extra", "  /ret", "/ret\n/run", " /",
])
def test_non_prefix_input_offers_no_candidates(typed):
    """Natural language, arguments, and multiline drafts must never open the menu."""
    assert matches(typed) == []


def test_a_fully_typed_command_is_not_a_candidate():
    """Enter on a complete command submits it instead of re-accepting the same text."""
    assert matches("/new") == []
    assert matches("/help") == []


def test_busy_state_limits_candidates_to_read_only_commands():
    assert [item.name for item in matches("/", busy=True, limit=99)] == list(READONLY_COMMANDS)
    assert matches("/new", busy=True) == []
    assert matches("/ret", busy=True) == []


def test_argument_commands_keep_the_command_name_alone():
    """Menu and Tab completion must insert the same text; arguments follow the name."""
    for name in COMMAND_LIST:
        assert completion(name) == name
    assert completion("/unknown") == ""


@pytest.mark.parametrize("size", [(50, 24), (80, 24), (120, 40)])
def test_menu_opens_filters_and_stays_inside_the_window(session, size):
    async def scenario():
        app = MultiomicsApp(session)
        async with app.run_test(size=size) as pilot:
            editor = app.query_one("#chat-input", PromptEditor)
            editor.focus()
            editor.value = "/"
            await pilot.pause()
            await pilot.pause()
            assert app._menu.visible() and len(menu_rows(app)) == 6
            assert menu_rows(app)[0].startswith("❯ /help")
            editor.value = "/re"
            await pilot.pause()
            await pilot.pause()
            assert menu_names(app) == ["/report", "/retry", "/resume"]
            assert app.query_one("#input-card").region.bottom <= size[1]
            assert session.active_task is None

    asyncio.run(scenario())


def test_menu_moves_accepts_and_closes_without_submitting(session):
    async def scenario():
        app = MultiomicsApp(session)
        async with app.run_test(size=(100, 36)) as pilot:
            editor = app.query_one("#chat-input", PromptEditor)
            editor.focus()
            editor.value = "/re"
            await pilot.pause()
            await pilot.pause()
            assert menu_names(app) == ["/report", "/retry", "/resume"]
            assert menu_rows(app)[0].startswith("❯ /report")
            await pilot.press("down")
            await pilot.pause()
            assert menu_rows(app)[1].startswith("❯ /retry")
            await pilot.press("up")
            await pilot.pause()
            assert menu_rows(app)[0].startswith("❯ /report")
            await pilot.press("tab")
            await pilot.pause()
            # Accepting inserts the command; it never runs it.
            assert editor.text == "/report"
            assert not app._menu.visible()
            assert session.active_task is None
            # Escape closes the menu and leaves help closed.
            editor.value = "/ret"
            await pilot.pause()
            await pilot.pause()
            assert app._menu.visible()
            await pilot.press("escape")
            await pilot.pause()
            assert not app._menu.visible() and not app._help_showing()
            assert editor.text == "/ret"

    asyncio.run(scenario())


def test_enter_submits_a_complete_command_without_accepting_it_again(session):
    async def scenario():
        app = MultiomicsApp(session)
        async with app.run_test(size=(100, 36)) as pilot:
            editor = app.query_one("#chat-input", PromptEditor)
            editor.focus()
            editor.value = "/new"
            await pilot.pause()
            await pilot.pause()
            # A fully typed command is not a candidate, so Enter runs it immediately.
            assert not app._menu.visible()
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()
            assert editor.text == ""
            assert session.active_task is not None
            # Accepting a prefix still needs a second Enter to run the command.
            editor.value = "/re"
            await pilot.pause()
            await pilot.pause()
            assert app._menu.visible()
            await pilot.press("enter")
            await pilot.pause()
            assert editor.text == "/report" and not app._menu.visible()
            assert session.tasks.get(session.active_task)["status"] == "draft"

    asyncio.run(scenario())


def test_busy_switch_refilters_an_open_menu(session):
    """Entering a busy state must drop non-read-only candidates immediately."""
    async def scenario():
        app = MultiomicsApp(session)
        async with app.run_test(size=(100, 36)) as pilot:
            editor = app.query_one("#chat-input", PromptEditor)
            editor.focus()
            editor.value = "/r"
            await pilot.pause()
            await pilot.pause()
            assert menu_names(app) == ["/runs", "/report", "/retry", "/run", "/resume"]
            app._set_busy(True)
            await pilot.pause()
            await pilot.pause()
            assert menu_names(app) == ["/runs"]
            app._set_busy(False)
            editor.value = "/"
            await pilot.pause()
            await pilot.pause()
            assert len(menu_rows(app)) == 6

    asyncio.run(scenario())


def test_help_owns_escape_when_the_menu_is_hidden_with_it(session):
    """F1 hides the input row, so an open menu must not swallow the next Escape."""
    async def scenario():
        app = MultiomicsApp(session)
        async with app.run_test(size=(100, 36)) as pilot:
            editor = app.query_one("#chat-input", PromptEditor)
            editor.focus()
            editor.value = "/he"
            await pilot.pause()
            await pilot.pause()
            assert app._menu.visible()
            await pilot.press("f1")
            await pilot.pause()
            assert app._help_showing() and not app._menu.visible()
            await pilot.press("escape")
            await pilot.pause()
            assert not app._help_showing()
            await pilot.press("tab")
            await pilot.pause()
            assert editor.text == "/help"

    asyncio.run(scenario())


def test_new_command_returns_to_the_welcome_screen(session):
    async def scenario():
        app = MultiomicsApp(session)
        async with app.run_test(size=(100, 36)) as pilot:
            editor = app.query_one("#chat-input", PromptEditor)
            editor.focus()
            editor.value = "/new"
            await pilot.pause()
            await pilot.pause()
            assert not app._menu.visible()
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()
            assert app._home_visible is True
            assert app.query_one("#home-hero").display is True
            assert not app.query_one("#workspace-scroll").display
            assert not app.query_one("#request-header").display
            task_id = session.active_task
            assert task_id and session.tasks.get(task_id)["status"] == "draft"
            # The draft survives the view reset, so /retry can continue it.
            assert session.handle("/retry").kind == "text"

    asyncio.run(scenario())


def test_new_from_a_task_view_returns_home_and_keeps_history(session):
    async def scenario():
        app = MultiomicsApp(session)
        async with app.run_test(size=(100, 36)) as pilot:
            first = session.tasks.create("保留的历史请求")
            session.active_task = first["task_id"]
            app._enter_workspace()
            await pilot.pause()
            assert app._home_visible is False
            await app._handle_async("/new", submitted=True)
            await pilot.pause()
            assert app._home_visible is True
            assert session.active_task != first["task_id"]
            # The earlier task is still on disk and selectable.
            assert session.tasks.get(first["task_id"])["status"] == "draft"

    asyncio.run(scenario())

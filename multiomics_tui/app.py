"""Single-page Textual interface for planning and running analysis tasks."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from rich.markup import escape
from rich.style import Style
from rich.text import Text
from textual.app import App, ComposeResult, ScreenStackError
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.events import Click
from textual.message import Message
from textual.suggester import SuggestFromList
from textual.widgets import Button, Collapsible, Input, RichLog, Static

from .prompt_editor import PromptEditor
from .controller import HELP_TEXT, READONLY_COMMANDS, Response, Session, planning_failure_message
from .task_planner import render_plan
from .language import choose
from .task_sessions import redact_secrets

# Explicit bitmap fonts keep the name recognizable without a font dependency.
_GLYPHS = {
    "A": (" ███ ", "█   █", "█████", "█   █", "█   █"),
    "C": (" ████", "█    ", "█    ", "█    ", " ████"),
    "E": ("█████", "█    ", "████ ", "█    ", "█████"),
    "G": (" ████", "█    ", "█ ███", "█   █", " ████"),
    "I": ("█████", "  █  ", "  █  ", "  █  ", "█████"),
    "L": ("█    ", "█    ", "█    ", "█    ", "█████"),
    "M": ("█   █", "██ ██", "█ █ █", "█   █", "█   █"),
    "N": ("█   █", "██  █", "█ █ █", "█  ██", "█   █"),
    "O": (" ███ ", "█   █", "█   █", "█   █", " ███ "),
    "S": (" ████", "█    ", " ███ ", "    █", "████ "),
    "T": ("█████", "  █  ", "  █  ", "  █  ", "  █  "),
    "U": ("█   █", "█   █", "█   █", "█   █", " ███ "),
}
_COMPACT_GLYPHS = {
    "A": (" █ ", "█ █", "███", "█ █", "█ █"),
    "C": (" ██", "█  ", "█  ", "█  ", " ██"),
    "E": ("███", "█  ", "██ ", "█  ", "███"),
    "G": (" ██", "█  ", "█ █", "█ █", " ██"),
    "I": ("███", " █ ", " █ ", " █ ", "███"),
    "L": ("█  ", "█  ", "█  ", "█  ", "███"),
    "M": ("█ █", "███", "███", "█ █", "█ █"),
    "N": ("█ █", "███", "███", "███", "█ █"),
    "O": (" █ ", "█ █", "█ █", "█ █", " █ "),
    "S": (" ██", "█  ", " █ ", "  █", "██ "),
    "T": ("███", " █ ", " █ ", " █ ", " █ "),
    "U": ("█ █", "█ █", "█ █", "█ █", "███"),
}


def _home_banner(glyphs: dict[str, tuple[str, ...]]) -> str:
    def word(value):
        return [" ".join(glyphs[letter][row] for letter in value) for row in range(5)]
    rows = word("MULTIOMICS") + [""] + word("AGENT")
    rows += ["", "Request → Plan → Confirm → Execute"]
    width = max(map(len, rows))
    return "\n".join(row.center(width) for row in rows)


BANNER = _home_banner(_GLYPHS)
COMPACT_BANNER = _home_banner(_COMPACT_GLYPHS)

# Slash-command completion list: read-only commands first, then the rest.
COMMAND_LIST = list(READONLY_COMMANDS) + ["/key", "/model", "/ask", "/new", "/cd", "/session", "/sessions",
                                          "/approve", "/retry", "/cancel", "/quit", "/run", "/data",
                                          "/resume", "/verify", "/dossiers",
                                          "/verify-dossiers", "/module", "/kill"]
# Home onboarding examples shown when the terminal is tall enough; click fills the input.
HOME_EXAMPLES = (
    "Check the input datasets and summarize sample IDs",
    "分析某一模块的结果并生成一个新的图表模块",
    "Ask what evidence exists for a target gene",
)


def _clickable_labelled(label: str, reference: str, action: str, *, label_style: str,
                        text_style: str) -> Text:
    """One clickable home-example row; the click runs an app action with the row label."""
    line = Text("▸ ", style=label_style)
    line.append(f"{label}   {reference}", style=Style.from_meta({"@click": action}))
    line.stylize(text_style)
    return line


class _CommandSuggester(SuggestFromList):
    """Suggest slash commands; only read-only commands while a request is running."""

    def __init__(self, app: "MultiomicsApp") -> None:
        super().__init__(COMMAND_LIST, case_sensitive=False)
        self._app = app

    async def get_suggestion(self, value: str) -> str | None:
        allowed = list(READONLY_COMMANDS) if self._app._busy else COMMAND_LIST
        for command in allowed:
            if command.startswith(value):
                return command
        return None


class HelpCloseRequested(Message):
    """Posted by the inline close button; actions on child widgets are not forwarded to the app."""


class MultiomicsApp(App):
    CSS_PATH = "styles.tcss"
    TITLE = "multiomics-agent"
    SUB_TITLE = "Multiomics workspace"
    BINDINGS = [
        Binding("ctrl+q", "quit_app", "Quit", priority=True),
        Binding("ctrl+r", "refresh_all", "Refresh", priority=True),
        Binding("f1", "show_help", "Help", priority=True),
        Binding("escape", "close_help", "Close help", priority=True),
    ]

    # Fixed home layout heights used to centre the mark without stale region reads.
    _HOME_INPUT_ROWS = 5      # input card: border + input + margin + prompt meta
    _HOME_STATUS_ROWS = 1
    _HOME_EXAMPLES_ROWS = 4   # three example rows plus their bottom margin

    def __init__(self, session: Session):
        super().__init__()
        self.session = session
        # A TUI launch starts at Home; saved tasks require an explicit /session.
        self.session.active_task = None
        self.selected_job: str | None = None
        self._log_offset = 0
        self._tick_timer = None
        self._busy = False
        self._busy_start = 0.0
        self._busy_action = "Working"
        self._busy_notice = False
        self._stage_label = ""
        self._editing_task: str | None = None
        self._editing_hash: str | None = None
        self._shown_task: str | None = None
        self._shown_hash: str | None = None
        self._pending_response: Response | None = None
        self._request_text = ""
        self._view_task = None
        self._last_terminal: tuple[str, str] | None = None
        self._home_visible = True
        self._banner_height = 0
        self.sub_title = f"Multiomics workspace · {session.root.name or session.root}"

    def compose(self) -> ComposeResult:
        with Vertical(id="home-hero"):
            yield Static(BANNER, id="welcome-banner", markup=False)
        with Horizontal(id="examples-row"):
            yield Vertical(id="home-examples")
        with Horizontal(id="request-header"):
            with Vertical(id="header-column"):
                yield Static("", id="request-preview")
                with Vertical(id="request-details-box"):
                    with Collapsible(title="Full request", id="request-details"):
                        yield Static("", id="request-full")
                with Horizontal(id="phase-row"):
                    yield Static("", id="phase-bar")
                    yield Static("", id="stage-title", markup=False)
        with VerticalScroll(id="workspace-scroll"):
            with Vertical(id="workspace-content"):
                with Vertical(id="main-sections"):
                    yield Static("", id="reply-message")
                    yield Static("", id="plan-preview")
                    yield Static("", id="progress-details")
                    with Horizontal(id="progress-actions"):
                        yield Button("Stop task", variant="error", id="stop-task")
                    yield Static("", id="result-summary")
                    with Collapsible(title="Sources", id="sources-panel"):
                        yield Static("", id="source-details")
                    with Collapsible(title="Details", id="details-panel"):
                        yield Static("", id="technical-details")
                    with Collapsible(title="Logs", id="logs-panel"):
                        yield RichLog(id="job-log", markup=True, wrap=True, highlight=False)
                    with Collapsible(title="History", id="history-panel"):
                        yield RichLog(id="chat-log", markup=True, wrap=True, highlight=False)
                yield Static(self.session.help_text(), id="help-text")
                yield Button("Close help", id="help-close")
        with Horizontal(id="inline-row"):
            with Vertical(id="inline-panels"):
                yield Static("", id="action-message")
                with Horizontal(id="action-buttons"):
                    yield Button("Confirm action", variant="warning", id="action-confirm")
                    yield Button("Cancel", id="action-cancel")
                yield Static("Enter this endpoint's API key. It stays in memory.", id="key-message")
                with Horizontal(id="key-controls"):
                    yield Input(password=True, placeholder="API key", id="key-input")
                    yield Button("Cancel", id="key-cancel")
        with Horizontal(id="input-row"):
            with Vertical(id="input-card"):
                with Vertical(id="action-dock"):
                    with Vertical(id="plan-actions"):
                        yield Static("", id="plan-hint")
                        with Horizontal(id="plan-buttons"):
                            yield Button("Confirm and run", variant="success", id="execute-plan")
                            yield Button("Revise plan", id="modify-plan")
                with Horizontal(id="prompt-controls"):
                    yield PromptEditor(suggester=_CommandSuggester(self),
                                       submit_handler=self._submit_prompt, id="chat-input")
                    yield Button("Send", id="send-prompt", variant="primary")
                yield Static("", id="prompt-meta")
        with Horizontal(id="footer-row"):
            yield Static("", id="status-bar")
        yield Static("", id="home-bottom-space")

    def on_mount(self) -> None:
        self._show("#action-message", False)
        self._show("#action-buttons", False)
        self._show("#key-message", False)
        self._show("#key-controls", False)
        self._show("#help-text", False)
        self._show("#help-close", False)
        self._show("#main-sections", False)
        self._show("#request-header", False)
        self._show("#sources-panel", False)
        self._show("#reply-message", False)
        self._show("#request-details-box", False)
        self._show("#workspace-scroll", False)
        self._show("#plan-actions", False)
        self._show("#progress-actions", False)
        self._fill_examples()
        self._refresh_banner()
        self._refresh_task_view()
        self._refresh_status()
        self._tick_timer = self.set_interval(2.0, self._tick)
        chat_input = self._get("#chat-input", PromptEditor)
        if chat_input is not None:
            chat_input.focus()

    def on_unmount(self) -> None:
        if self._tick_timer is not None:
            self._tick_timer.stop()
            self._tick_timer = None

    def on_resize(self, event) -> None:
        self._resize_prompt()
        self._refresh_banner()
        self._refresh_status()
        self._refresh_home_extras()

    def _fill_examples(self) -> None:
        """Render clickable onboarding examples; clicking fills the input without submitting."""
        container = self._get("#home-examples")
        if container is None:
            return
        try:
            container.remove_children()
            for text in HOME_EXAMPLES:
                line = Text("▸ ", style="bold #4a9eff")
                line.append(text, style=Style.from_meta({"@click": f"example({text!r})"}))
                line.stylize("#c7d7ea")
                container.mount(Static(line, markup=False))
        except NoMatches:
            return

    def action_example(self, text: str) -> None:
        """Fill the shared input with an example request; the user still submits it."""
        chat_input = self._get("#chat-input", PromptEditor)
        if chat_input is None or self._busy:
            return
        chat_input.value = text
        chat_input.focus()

    def _refresh_banner(self) -> None:
        banner = self._get("#welcome-banner", Static)
        if banner is None:
            return
        value = BANNER if self.size.width >= 64 else COMPACT_BANNER
        rows = value.splitlines()
        width = max(map(len, rows))
        # Reserve the five-row input/footer and a row of breathing room above/below.
        minimum = max(map(len, COMPACT_BANNER.splitlines())) + 4
        self._banner_ok = width + 4 <= self.size.width and len(rows) + 7 <= self.size.height
        if not self._banner_ok:
            value = f"Enlarge terminal (need {minimum}×{len(rows) + 7}, now {self.size.width}×{self.size.height})"
            rows = [value]
            width = len(value)
        mark = Text()
        for index, row in enumerate(rows):
            if index:
                mark.append("\n")
            color = "#b5b5b5" if index < 6 or index >= 11 else "#eeeeee"
            mark.append(row, style=color)
        banner.styles.width = width
        banner.styles.height = len(rows)
        self._banner_height = len(rows)
        banner.update(mark)
        self._refresh_home_extras()

    def _refresh_home_extras(self) -> None:
        """Show the example rows only when the home view can also centre the mark."""
        show = (self._home_visible and getattr(self, "_banner_ok", True)
                and self.size.height >= 32)
        row = self._get("#examples-row")
        if row is not None:
            row.display = show
            # A hidden child still reserves auto height; collapse the row explicitly.
            row.styles.height = "auto" if show else 0
        self._center_home_banner()

    def _center_home_banner(self) -> None:
        """Centre the mark in the window, not merely in the space above the input.

        The bottom cluster (input card + status bar, plus the example block when shown)
        has a known height, so a top padding equal to it puts the mark's centre at the
        window centre. Region measurements are deliberately avoided here: they are stale
        during a resize until the next layout pass.
        """
        hero = self._get("#home-hero")
        if hero is None:
            return
        if not self._home_visible or not self._banner_height:
            hero.styles.padding = (0, 0, 0, 0)
            return
        prompt = self._get("#chat-input", PromptEditor)
        editor_rows = int(prompt.styles.height.value) if prompt is not None and prompt.styles.height else 2
        below = self._HOME_INPUT_ROWS + editor_rows - 2 + self._HOME_STATUS_ROWS
        row = self._get("#examples-row")
        if row is not None and row.display:
            below += self._HOME_EXAMPLES_ROWS
        hero_h = self.size.height - below
        padding = max(0, min(below, hero_h - self._banner_height))
        # This Textual version lays out from the ``padding`` shorthand; padding_top alone is ignored.
        hero.styles.padding = (padding, 0, 0, 0)

    def _say(self, english: str, chinese: str) -> str:
        return choose(self.session.reply_language(), english, chinese)

    def _request(self, value: str) -> None:
        """Show the pinned request header: preview, optional full text, stage line."""
        self._request_text = redact_secrets(value)
        from rich.cells import cell_len
        # Three rows maximum for the pinned preview keeps small terminals usable.
        budget = max(20, self.size.width - 4) * 3 - 2
        preview = ""
        for character in self._request_text.replace("\n", " "):
            if cell_len(preview + character) > budget:
                break
            preview += character
        truncated = len(preview) < len(self._request_text)
        self._update("#request-preview", preview + ("…" if truncated else ""))
        self._update("#request-full", self._request_text)
        self._show("#request-details-box", truncated)
        self._refresh_workspace()

    def _enter_workspace(self) -> None:
        self._home_visible = False
        self._refresh_workspace()

    def _refresh_workspace(self) -> None:
        """Single place that decides which top-level regions are visible."""
        help_showing = self._help_showing()
        if self._home_visible and not help_showing:
            self._show("#home-hero", True)
            self._show("#home-bottom-space", True)
            self._show("#workspace-scroll", False)
            self._show("#request-header", False)
            self._show("#main-sections", False)
        else:
            self._show("#home-hero", False)
            self._show("#home-bottom-space", False)
            self._show("#workspace-scroll", True)
            self._show("#request-header", not help_showing)
            self._show("#main-sections", not help_showing)
        if self._home_visible and not help_showing:
            self._show_dock(False)
        self._refresh_home_extras()

    def _set_stage(self, value: str, level: str = "neutral") -> None:
        """Update the stage line and its severity colour."""
        widget = self._get("#stage-title", Static)
        if widget is None:
            return
        widget.update(escape(value))
        for name in ("stage-ok", "stage-busy", "stage-warn", "stage-error", "stage-neutral"):
            widget.set_class(f"stage-{level}" == name, name)

    def _update_phase(self, task_id: str | None, status: str) -> None:
        """Render the four-step phase indicator; phases before the current one are done."""
        widget = self._get("#phase-bar", Static)
        if widget is None:
            return
        failure_stage = None
        if status in {"failed", "killed"}:
            try:
                failure_stage = self.session.tasks.get(task_id).get("failure_stage") if task_id else None
            except (OSError, ValueError):
                pass
            rank = {"generation":2,"preflight":2,"execution":2,"verification":3}.get(failure_stage, 0)
        elif status == "succeeded":
            rank = 3
        elif status in {"approved", "generating_code", "running", "verifying"}:
            rank = 2
        elif status == "awaiting_approval":
            rank = 1
        else:
            rank = 0
        _PHASES = (("Plan", "plan"), ("Confirm", "confirm"), ("Run", "run"), ("Result", "result"))
        mark = Text()
        for index, (name, key) in enumerate(_PHASES):
            if index:
                mark.append("  ")
            active = index == rank
            if status in {"failed", "killed"} and index == 2:
                mark.append("● Run failed" if failure_stage else "○ Run", style="#ff8a8a")
            elif status in {"failed", "killed"} and failure_stage is None:
                mark.append("○ " + name, style="#666666")
            elif index < rank:
                mark.append("✔ " + name, style="#6fdd8b")
            elif active:
                style = {"failed": "#ff8a8a", "killed": "#ffb46b"}.get(status, "#7ab7ff")
                mark.append("● " + name, style=f"{style} bold")
            else:
                mark.append("○ " + name, style="#666666")
        widget.update(mark)

    def _help_showing(self) -> bool:
        widget = self._get("#help-text")
        return bool(widget is not None and widget.display)

    def _get(self, selector, expect_type=None):
        """Return a widget only while the base screen is attached."""
        if not self.is_running:
            return None
        try:
            screen = self.screen_stack[0]
        except ScreenStackError:
            return None
        for widget in screen.query(selector):
            if expect_type is None or isinstance(widget, expect_type):
                return widget
        return None

    def _show(self, selector: str, visible: bool) -> None:
        widget = self._get(selector)
        if widget is not None:
            widget.display = visible

    def _show_dock(self, visible: bool) -> None:
        """Show or hide the fixed action dock; hidden rows must not reserve height."""
        dock = self._get("#action-dock")
        if dock is None:
            return
        dock.display = visible
        dock.styles.height = "auto" if visible else 0

    def _reveal(self, selector: str, *, top: bool = False) -> None:
        widget = self._get(selector)
        if widget is not None:
            widget.scroll_visible(animate=False, immediate=True, top=top)

    def _update(self, selector: str, value: str) -> None:
        widget = self._get(selector, Static)
        if widget is not None:
            widget.update(escape(value))

    def _write_chat(self, value: str) -> None:
        log = self._get("#chat-log", RichLog)
        if log is not None:
            log.write(value)

    def _refresh_status(self) -> None:
        self._update("#prompt-meta", "Enter Send · Ctrl+J Newline · /new")
        service = ("AI checked" if self.session.connection_tested() else "AI configured") if self.session.llm_mode == "LLM" else \
            ("AI on · /key" if self.session.key_available() else "AI off · /key")
        cfg = self.session.cfg.get('llm', {})
        if self.session.cfg.get('_credential_error'):
            service = 'Key vault unavailable · /key'
        service += ' · ' + cfg.get('provider', 'deepseek') + '/' + cfg.get('model', '')
        if self._busy:
            context = f"Working… {self._format_elapsed()} · read-only / commands only"
        elif self._home_visible:
            workspace = self.session.root.name or str(self.session.root)
            context = f"Workspace {workspace} · Enter a request in English or Chinese"
        else:
            context = self._stage_label or "Task session"
        if self.size.width < 68:
            # Put the selected model first; the narrow footer otherwise hides it.
            status = service
        else:
            status = f"{service} · F1 Help · /model · /sessions · Ctrl+Q Quit · Ctrl+R Refresh · {context}"
        self._update("#status-bar", status)

    def _format_elapsed(self) -> str:
        seconds = max(0, int(time.monotonic() - self._busy_start))
        if seconds < 60:
            return f"{seconds}s"
        return f"{seconds // 60}m {seconds % 60:02d}s"

    def _refresh_stage(self, label: str, level: str, busy: bool) -> None:
        self._stage_label = label
        self._set_stage(f"{label} · {self._format_elapsed()}" if busy else label, level)

    def _tick(self) -> None:
        if not self.is_running:
            return
        try:
            if self._busy:
                self._set_stage(f"{self._stage_label} · {self._format_elapsed()}", "busy")
            self._refresh_task_view()
            self._follow_job()
            self._refresh_status()
        except NoMatches:
            return

    def _stop_selected_job(self) -> None:
        """Stop button: reuses the /kill confirmation flow, never signals without approval."""
        if self._busy:
            return
        job_id = self.selected_job
        if not job_id:
            state = self.session.tasks.get(self.session.active_task) if self.session.active_task else None
            job_id = (state or {}).get("job_id")
        if not job_id:
            self._message(self._say("No running job is selected.", "当前没有可停止的任务。"))
            return
        response = self.session.kill_job(job_id)
        if response.kind == "need_confirm":
            self._pending_response = response
            self._update("#action-message", response.message)
            self._show("#action-message", True)
            self._show("#action-buttons", True)
            self._reveal("#action-message", top=True)
        else:
            self._message(response.message)

    def _select_job(self, job_id: str) -> None:
        if self.selected_job == job_id:
            return
        self.selected_job = job_id
        self._log_offset = 0
        log = self._get("#job-log", RichLog)
        if log is not None:
            log.clear()
        self._follow_job()

    def _follow_job(self) -> None:
        if not self.selected_job:
            return
        path = self.session.jobs.log_path(self.selected_job)
        if not path.is_file():
            return
        try:
            size = path.stat().st_size
        except OSError:
            return
        if size < self._log_offset:
            self._log_offset = 0
        if size == self._log_offset:
            return
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(self._log_offset)
            chunk = handle.read()
            self._log_offset = handle.tell()
        widget = self._get("#job-log", RichLog)
        if widget is not None:
            for line in chunk.splitlines():
                widget.write(escape(line))

    def _refresh_task_view(self) -> None:
        task_id = self.session.active_task
        if self._view_task != task_id:
            # Invalidate the cached plan view whenever the shown task changes.
            self._shown_task = None
            self._shown_hash = None
        modify = self._get("#modify-plan", Button)
        execute = self._get("#execute-plan", Button)
        if not task_id:
            self._update_phase(None, "draft")
            if not self._help_showing():
                self._update("#stage-title", "")
            for selector in ("#plan-preview", "#plan-actions", "#progress-details",
                             "#progress-actions", "#result-summary", "#logs-panel"):
                self._show(selector, False)
            self._show_dock(False)
            if modify is not None:
                modify.disabled = True
            if execute is not None:
                execute.disabled = True
            return
        try:
            state = self.session.tasks.reconcile(task_id, self.session.jobs)
        except (OSError, ValueError) as exc:
            self._show("#plan-preview", True)
            self._update("#plan-preview", self._say("Could not load task: ", "无法读取任务：") + str(exc))
            self._update("#result-summary", self._say("No verified result available.", "暂无可用结果。"))
            self._set_stage(self._say("Could not load task", "无法读取任务"), "error")
            return
        status = state["status"]
        if self._view_task != task_id:
            self._view_task = task_id
            self.selected_job = None
            self._log_offset = 0
            log = self._get("#job-log", RichLog)
            if log is not None:
                log.clear()
            for selector in ("#history-panel", "#logs-panel", "#details-panel", "#sources-panel"):
                widget = self._get(selector, Collapsible)
                if widget is not None:
                    widget.collapsed = True
        self._request(self.session.tasks.original_request(task_id))
        editable = status in {"draft", "blocked", "needs_clarification", "awaiting_approval"}
        stage = {
            "draft": ("Preparing plan", "warn"),
            "blocked": ("Planning blocked", "error"),
            "needs_clarification": ("Clarification needed", "warn"),
            "awaiting_approval": ("Awaiting confirmation", "warn"),
            "approved": ("Starting", "busy"),
            "generating_code": ("Preparing execution", "busy"),
            "running": ("Running", "busy"),
            "verifying": ("Verifying outputs", "busy"),
            "succeeded": ("Completed", "ok"),
            "failed": ("Failed", "error"),
            "killed": ("Stopped", "warn"),
        }
        label, level = stage.get(status, (status, "neutral"))
        self._update_phase(task_id, status)
        self._set_stage(label, level)
        self._show("#plan-preview", editable and not self._busy)
        self._show("#plan-actions", status == "awaiting_approval" and not self._busy)
        help_open = self._help_showing()
        self._show_dock(status == "awaiting_approval" and not self._busy and not help_open)
        if status == "awaiting_approval" and not self._busy and not help_open:
            # Keep the primary action on screen even when the plan itself scrolls.
            confirm = self._get("#execute-plan", Button)
            if confirm is not None:
                confirm.scroll_visible(animate=False, immediate=True)
        self._show("#progress-details", not editable and status not in {"succeeded", "failed", "killed"})
        self._show("#progress-actions",
                   status in {"approved", "generating_code", "running", "verifying"}
                   and bool(state.get("job_id")))
        self._show("#result-summary", status in {"succeeded", "failed", "killed"})
        self._show("#logs-panel", bool(state.get("job_id")))
        self._show("#details-panel", True)
        if self._help_showing():
            # The help view owns the workspace; task panels are restored when it closes.
            self._show("#request-header", False)
            self._show("#main-sections", False)
        cfg = self.session.cfg.get('llm', {})
        model = cfg.get("model", "unconfigured")
        key_status = 'not required' if cfg.get('auth_mode') == 'none' else ('available' if self.session.key_available() else 'missing')
        meta = f"Task: {task_id}\nProvider: {cfg.get('provider', 'deepseek')}\nModel: {model}\nEndpoint: {cfg.get('base_url', '')}\nKey: {key_status}\nProject: {self.session.root}"
        self._update("#technical-details", meta + f"\nPlan SHA256: {state.get('plan_hash') or 'pending'}")
        if state.get("error"):
            self._update("#technical-details", meta + f"\nPlan SHA256: {state.get('plan_hash') or 'pending'}"
                         + "\n" + state["error"])
        if status == "awaiting_approval":
            try:
                plan = self.session.tasks.plan(task_id)
                self._shown_task = task_id
                self._shown_hash = state["plan_hash"]
                preview = render_plan(task_id, plan, state["plan_hash"], language=self.session.reply_language(), compact=True)
                if plan["goal"] == self.session.tasks.original_request(task_id):
                    preview = preview.split("\n\n", 1)[1]
                self._update("#technical-details", meta + "\n" + render_plan(task_id, plan, state["plan_hash"]))
            except (OSError, ValueError) as exc:
                preview = self._say("Plan cannot be loaded: ", "无法读取计划：") + str(exc)
        elif status == "needs_clarification":
            questions = state.get("questions") or []
            preview = "\n".join(questions) + self._say("\nReply below to continue.", "\n请在下方补充说明。")
        elif status == "blocked":
            kind = state.get("planning_failure_kind")
            # Old blocked sessions have no typed failure metadata.
            if not kind and "LLMValidationError" in (state.get("error") or ""):
                kind = "validation"
            preview = planning_failure_message(kind, self.session.reply_language())
        elif status == "draft":
            preview = self._say("Enter the task request to start planning.", "请输入任务要求以开始制定计划。")
        else:
            preview = f"Task {task_id}: {status}. The approved plan is locked."
        self._update("#plan-preview", preview)
        self._refresh_stage(self._busy_action if self._busy and editable else label,
                            "busy" if self._busy and editable else level,
                            self._busy and editable)
        hint = self._get("#plan-hint", Static)
        if hint is not None:
            note = self._say("Confirm to run; nothing runs before confirmation.",
                             "点击 Confirm and run 才会执行；确认前不会运行。")
            # On very short windows the hint row is dropped and the plan scrolls instead.
            show_hint = status == "awaiting_approval" and self.size.height >= 30
            hint.update(escape(note) if show_hint else "")
            hint.display = show_hint
        if modify is not None:
            modify.disabled = self._busy or self._shown_task is None
        if execute is not None:
            # Only an awaiting plan that is displayed on screen may be executed.
            execute.disabled = (self._busy or self._shown_task is None
                                or status != "awaiting_approval")
        if state.get("job_id") and not self.selected_job:
            self._select_job(state["job_id"])
        self._update_progress(state)
        # Terminal states show the verified summary; other states keep a placeholder in place.
        self._update("#result-summary", self._task_summary(state))
        terminal = (task_id, status) if status in {"succeeded", "failed", "killed"} else None
        if terminal and terminal != self._last_terminal:
            result = self._get("#result-summary", Static)
            if result is not None:
                result.scroll_visible(animate=False, immediate=True)
        self._last_terminal = terminal

    def _update_progress(self, state: dict | None) -> None:
        parts = []
        if state is not None:
            phase = {
                "draft": "Draft", "blocked": "Planning failed",
                "needs_clarification": "Needs clarification",
                "awaiting_approval": "Awaiting confirmation",
                "approved": "Starting", "generating_code": "Preparing execution",
                "running": "Running", "verifying": "Verifying outputs",
                "succeeded": "Completed", "failed": "Failed", "killed": "Stopped",
            }.get(state["status"], state["status"])
            parts.append(f"Phase: {phase}")
            path = self.session.tasks.directory(state["task_id"]) / "events.jsonl"
            events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.is_file() else []
            steps = [e for e in events if e["role"] == "system"
                     and e["content"].startswith(("Starting step ", "Completed step "))]
            if steps:
                ordered, latest = [], ""
                for entry in steps:
                    step_id = entry["content"].split()[2].rstrip(";")
                    if step_id not in ordered:
                        ordered.append(step_id)
                    latest = f"Completed: {step_id}" if entry["content"].startswith("Completed") else f"Running: {step_id}"
                parts.append(f"Steps: {len(ordered)} started · last {latest}")
            if state.get("error"):
                parts.append(self._say("Error: ", "错误：") + str(state["error"]))
        if self.selected_job:
            job = self.session.jobs.get(self.selected_job)
            if job:
                parts.append(f"Job: {job['status']}")
        self._update("#progress-details", "\n".join(parts))

    def _task_summary(self, state: dict) -> str:
        task_id = state["task_id"]
        task_dir = (self.session.root / "analysis" / task_id).resolve()
        status = state["status"]
        if status in {"failed", "killed", "blocked"}:
            lines = [self._say(f"Task {status}.", "任务未完成：" + {"failed": "失败", "killed": "已停止", "blocked": "受阻"}[status])]
            stage = state.get("failure_stage")
            labels = {"generation":"代码生成", "preflight":"预检", "execution":"分析执行", "verification":"产物验证"}
            lines.append(self._say("Failure stage: " + (stage or "unknown"), "失败阶段：" + labels.get(stage,"未知")))
            if state.get("failed_step"):
                lines.append(self._say("Step: ", "步骤：") + state["failed_step"])
            if state.get("error"):
                lines.append(self._say("Error: ", "错误：") + state["error"])
            if state.get("job_id"):
                lines.append(self._say("Job log: ", "任务日志：") + str(self.session.jobs.log_path(state["job_id"])))
            if task_dir.is_dir():
                lines.append(self._say("Data directory: ", "产物目录：") + str(task_dir))
                failure = task_dir / "reports/failure.txt"
                if failure.is_file():
                    lines.append(self._say("Failure details: ", "失败详情：") + str(failure))
            if state.get("job_id"):
                lines.append(self._say(f"Next: run /tail {state['job_id']} for the log, then revise the plan or /new.",
                                       f"下一步：可用 /tail {state['job_id']} 查看日志，然后修改计划或使用 /new。"))
            else:
                lines.append(self._say("Next: revise the plan or run /new for a new task.",
                                       "下一步：请修改计划，或使用 /new 开启新任务。"))
            return "\n".join(lines)
        if status != "succeeded":
            return f"No completed result for {task_id}."
        manifest_path = task_dir / "result_manifest.json"
        report_path = task_dir / "reports/task_report.md"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if (manifest.get("task_id") != task_id or
                    manifest.get("plan_hash") != state.get("approved_hash") or
                    not report_path.is_file() or not isinstance(manifest.get("outputs"), list)):
                raise ValueError("result manifest or report does not match the approved task")
            outputs = manifest["outputs"]
            for item in outputs:
                relative = Path(item["path"])
                if (relative.is_absolute() or ".." in relative.parts or
                        not (task_dir / relative).is_file() or
                        (task_dir / relative).stat().st_size != item["bytes"]):
                    raise ValueError(f"output is missing or changed: {relative}")
            plan = self.session.tasks.plan(task_id)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return (self._say("Task finished, but output inventory verification failed: ", "任务已结束，但产物清单检查未通过：") + str(exc) + "\n" +
                    self._say("Data directory: ", "产物目录：") + str(task_dir) + "\n" +
                    self._say("Job log: ", "任务日志：") +
                    str(self.session.jobs.log_path(state["job_id"]) if state.get("job_id") else "unavailable"))
        lines = [self._say("Task completed. Output inventory checked.", "任务完成，产物清单检查通过。"), self._say("Data directory: ", "产物目录：") + str(task_dir),
                 self._say("Report: ", "报告：") + str(report_path), self._say("Outputs in manifest: ", "产物数量：") + str(len(outputs))]
        for item in outputs[:10]:
            dimensions = ""
            if item.get("rows") is not None and item.get("columns") is not None:
                dimensions = self._say(f" ({item['rows']} rows × {item['columns']} columns)", f"（{item['rows']} 行 × {item['columns']} 列）")
            lines.append(f"  {item['path']}{dimensions}")
        if len(outputs) > 10:
            lines.append(self._say(f"  ... and {len(outputs) - 10} more; see the report and manifest.", f"另有 {len(outputs) - 10} 个产物，详见报告及清单。"))
        if plan["limitations"]:
            lines.append(self._say("Limits: ", "限制：") + "; ".join(plan["limitations"]))
        lines.append(self._say("Next: review the report, then revise the plan or run /new for a new task.",
                               "下一步：请先阅读报告，然后可修改计划或使用 /new 开启新任务。"))
        return "\n".join(lines)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if not self.is_running:
            return
        if event.input.id == "key-input":
            if self._busy:
                return
            key = event.value.strip()
            event.input.value = ""
            self.run_worker(self._provide_key(key), exclusive=False)
            return

    def on_text_area_changed(self, event) -> None:
        if event.text_area.id == "chat-input":
            self._resize_prompt()

    def _resize_prompt(self) -> None:
        prompt = self._get("#chat-input", PromptEditor)
        if prompt is not None:
            # Keep room for the plan and buttons even in short terminals.
            maximum = max(2, min(6, self.size.height // 5))
            prompt.styles.height = max(2, min(maximum, prompt.wrapped_document.height))
            self._center_home_banner()

    def _submit_prompt(self, prompt: PromptEditor) -> None:
        if not self.is_running or not prompt.text.strip():
            return
        value = prompt.text
        if self._busy:
            if value.lstrip().startswith("/") and value.split()[0].lower() in READONLY_COMMANDS:
                prompt.value = ""
                self.run_worker(self._handle_readonly(value), exclusive=False)
            elif not self._busy_notice:
                self._busy_notice = True
                self._message(self._say(
                    "Still working on the current request; only read-only commands run now.",
                    "当前请求仍在处理：此时仅可执行只读命令，其它输入请在完成后提交。"))
            return
        prompt.value = ""
        # Reserve the worker before another Enter/click can queue the same request.
        self._set_busy(True)
        self.run_worker(self._handle_async(value, submitted=True), exclusive=False)

    async def _handle_async(self, value: str, *, submitted: bool = False) -> None:
        if not self.is_running or (self._busy and not submitted):
            return
        if value.strip() == '/model':
            self._set_busy(False)
            await self._render(self.session.handle_tui(value))
            return
        head = value.strip().split(maxsplit=1)
        if head and head[0].lower() == '/cd':
            self._busy_action = "Checking workspace"
            self._set_busy(True)
            # /cd is the foreground operation; do not let its own busy flag look
            # like a concurrent model request while it runs in a worker thread.
            self.session.model_busy = False
            try:
                response = await asyncio.to_thread(self.session.handle_tui, value)
                await self._render(response)
                if response.kind != "cwd" and response.message:
                    try:
                        self.notify(escape(response.message), title="Working directory", timeout=8)
                    except Exception:  # noqa: BLE001 - notification support is optional
                        pass
            except Exception as exc:  # noqa: BLE001 - keep the interface usable
                self._message(self._say("Command failed: ", "命令执行失败：") + f"{type(exc).__name__}: {exc}")
            finally:
                self._set_busy(False)
                self._refresh_task_view()
                self._refresh_status()
                chat_input = self._get("#chat-input", PromptEditor)
                if chat_input is not None and chat_input.is_attached:
                    chat_input.focus()
            return
        self._busy_action = "Planning" if not value.startswith("/") else "Running command"
        self._set_busy(True)
        self._enter_workspace()
        self._write_chat(f"[b cyan]You: [/b cyan]{escape(redact_secrets(value))}")
        self._request(value)
        self._show("#reply-message", False)
        self._show("#sources-panel", False)
        self._show("#plan-preview", False)
        self._show("#plan-actions", False)
        self._hide_action()
        self._hide_key()
        try:
            response = await asyncio.to_thread(self.session.handle_tui, value, self._editing_hash)
            self._editing_task = None
            self._editing_hash = None
            await self._render(response)
        except Exception as exc:  # noqa: BLE001 - keep the interface usable
            self._message(self._say("Internal error: ", "内部错误：") + f"{type(exc).__name__}: {exc}")
        finally:
            self._set_busy(False)
            self._refresh_task_view()
            self._refresh_status()
            chat_input = self._get("#chat-input", PromptEditor)
            if chat_input is not None and chat_input.is_attached:
                chat_input.focus()

    async def _handle_readonly(self, value: str) -> None:
        """Run a whitelisted read-only command while a planning or running request is active."""
        if not self.is_running:
            return
        self._write_chat(f"[b cyan]You: [/b cyan]{escape(redact_secrets(value))}")
        try:
            response = await asyncio.to_thread(self.session.readonly_command, value)
        except Exception as exc:  # noqa: BLE001 - keep the interface usable
            self._message(self._say("Command failed: ", "命令执行失败：") + f"{type(exc).__name__}: {exc}")
        else:
            if response.message:
                self._sync_message(response.message)
            else:
                self._message(self._say("Done.", "已完成。"))
        finally:
            self._refresh_task_view()
            self._refresh_status()

    def _sync_message(self, message: str) -> None:
        """Show command output on the workspace page and in the history log."""
        self._update("#reply-message", message)
        self._show("#reply-message", bool(message))
        self._write_chat(escape(message))

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.session.model_busy = busy
        if busy:
            self._busy_start = time.monotonic()
            self._busy_notice = False
        # Plan buttons keep their state from _refresh_task_view: a running task must stay disabled.
        for selector in ("#action-confirm", "#action-cancel", "#stop-task"):
            widget = self._get(selector)
            if widget is not None:
                widget.disabled = busy
        if busy:
            execute = self._get("#execute-plan", Button)
            if execute is not None:
                execute.disabled = True
            modify = self._get("#modify-plan", Button)
            if modify is not None:
                modify.disabled = True
        self._refresh_status()

    def _hide_action(self) -> None:
        self._pending_response = None
        self._show("#action-message", False)
        self._show("#action-buttons", False)
        self._show("#plan-actions", False)
        self._show_dock(False)

    def _hide_key(self) -> None:
        key_input = self._get("#key-input", Input)
        if key_input is not None:
            key_input.value = ""
        self._show("#key-message", False)
        self._show("#key-controls", False)

    def _message(self, message: str) -> None:
        self._update("#reply-message", message)
        self._show("#reply-message", bool(message))
        if message:
            self._write_chat(escape(message))

    def _apply_workspace_switch(self, response: Response) -> None:
        """Reset task-specific UI state after /cd changes the workspace root."""
        self._view_task = None
        self._shown_task = None
        self._shown_hash = None
        self._editing_task = None
        self._editing_hash = None
        self._selected_job = None
        self._log_offset = 0
        self._pending_response = None
        self._hide_action()
        self._hide_key()
        self._request("")
        for selector in ("#job-log", "#chat-log"):
            log = self._get(selector, RichLog)
            if log is not None:
                log.clear()
        self._home_visible = True
        self._update("#reply-message", "")
        self._show("#reply-message", False)
        self._refresh_workspace()
        self.sub_title = f"Multiomics workspace · {self.session.root.name or self.session.root}"
        self._refresh_status()
        self._refresh_task_view()
        self._write_chat(f"[b cyan]Workspace:[/b cyan] {escape(str(self.session.root))}")
        self._write_chat(escape(response.message))
        self._update("#reply-message", response.message)
        self._show("#reply-message", True)
        try:
            self.notify(escape(response.message), title="Workspace", timeout=8)
        except Exception:  # noqa: BLE001 - notification support is optional
            pass

    def _toggle_help(self, show: bool) -> None:
        if not self.is_running:
            return
        help_widget = self._get("#help-text")
        if help_widget is None:
            return
        self._show("#help-text", show)
        self._show("#help-close", show)
        # Focused reading: hide the bottom dock and input card while help is open.
        self._show("#input-row", not show)
        self._show("#inline-row", not show)
        if show:
            self._show_dock(False)
        self._refresh_workspace()
        if self._home_visible and not show:
            self._show_dock(False)
        if not show:
            self._refresh_task_view()
            if self._home_visible:
                self._show_dock(False)
            chat_input = self._get("#chat-input", PromptEditor)
            if chat_input is not None and chat_input.is_attached:
                chat_input.focus()

    async def _render(self, response: Response) -> None:
        if not self.is_running:
            return
        kind = response.kind
        if kind == "cwd":
            self._apply_workspace_switch(response)
            return
        if kind == 'model_settings':
            from .model_panel import ModelPanel
            def saved(message):
                self._refresh_status()
                if message:
                    self._message(message)
            self.push_screen(ModelPanel(self.session), saved)
            return
        if kind == "quit":
            self.exit()
            return
        if kind == "session":
            log = self._get("#chat-log", RichLog)
            if log is not None:
                log.clear()
            self._write_chat(escape(response.message))
            self.selected_job = None
            self._log_offset = 0
            job_log = self._get("#job-log", RichLog)
            if job_log is not None:
                job_log.clear()
            self._refresh_task_view()
            return
        if kind == "need_confirm":
            if response.action_id == "task_plan":
                self._write_chat(escape(response.message))
                self._show("#reply-message", False)
                self._refresh_task_view()
                self._reveal("#plan-preview", top=True)
            else:
                self._pending_response = response
                self._update("#action-message", response.message)
                self._show("#action-message", True)
                self._show("#action-buttons", True)
                self._reveal("#action-message", top=True)
            return
        if kind == "need_key":
            self._pending_response = response
            cfg = self.session.cfg.get('llm', {})
            self._update('#key-message', f"{cfg.get('provider', 'API')} / {cfg.get('model', '')}: " + response.message)
            self._show("#key-message", True)
            self._show("#key-controls", True)
            self._reveal("#key-controls")
            key_input = self._get("#key-input", Input)
            if key_input is not None:
                key_input.focus()
            return
        if kind == "job":
            self._write_chat(escape(response.message))
            if response.job:
                self._select_job(response.job["job_id"])
            self._refresh_task_view()
            return
        if kind == "excerpts":
            if getattr(response, "read_only", False):
                self._message(response.message)
                self._refresh_task_view()
                return
            if response.answer:
                message = response.answer + self._say("\nReview the cited sources.", "\n请对照所引来源核查。")
            elif not response.excerpts:
                message = self._say("No relevant source excerpts were found.", "未找到相关来源摘录。")
            elif response.notice and ("unavailable" in response.notice or "rejected" in response.notice):
                message = self._say("AI summary unavailable. Expand Sources to inspect the original excerpts.",
                                    "模型摘要不可用，请展开 Sources 查看原文。")
            else:
                message = self._say("Expand Sources to inspect original excerpts. Use /ask --summary for a summary.",
                                    "请展开 Sources 查看原文；使用 /ask --summary 可请求摘要。")
            self._message(message)
            sources = []
            for excerpt in response.excerpts:
                location = f"{excerpt['source']}:{excerpt['line']}" if excerpt.get("line") else excerpt["source"]
                sources.append(f"{excerpt['id']} {location}\n{excerpt['text']}")
            self._update("#source-details", "\n\n".join(sources))
            self._show("#sources-panel", bool(sources))
            return
        if getattr(response, "read_only", False):
            # Command output is shown on the workspace page, never pasted into the plan area.
            self._sync_message(response.message or self._say("Done.", "已完成。"))
            self._refresh_task_view()
            return
        if response.message:
            state = self.session.tasks.get(self.session.active_task) if self.session.active_task else None
            if state and state["status"] in {"needs_clarification", "blocked"}:
                self._write_chat(escape(response.message))
                self._show("#reply-message", False)
            else:
                self._message(response.message)
            if not state:
                self._set_stage(self._say("Answer", "答复"), "ok")
        self._refresh_task_view()
        if self.session.active_task:
            try:
                if self.session.tasks.get(self.session.active_task)["status"] == "needs_clarification":
                    self._reveal("#plan-preview", top=True)
            except (OSError, ValueError):
                pass

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if not self.is_running:
            return
        button_id = event.button.id
        if button_id == "send-prompt":
            self._submit_prompt(self.query_one("#chat-input", PromptEditor))
            return
        if self._busy:
            return
        if button_id == "stop-task":
            self._stop_selected_job()
        elif button_id == "modify-plan":
            if self._shown_task and self._shown_hash:
                self._editing_task = self._shown_task
                self._editing_hash = self._shown_hash
                self._message(self._say("Describe the change below.", "请在下方输入修改要求。"))
                chat_input = self._get("#chat-input", PromptEditor)
                if chat_input is not None:
                    chat_input.focus()
        elif button_id == "execute-plan":
            await self._execute_current_plan()
        elif button_id == "action-confirm":
            pending = self._pending_response
            self._hide_action()
            if pending:
                await self._run_pending(self.session.confirm, pending.pending_id)
        elif button_id == "action-cancel":
            pending = self._pending_response
            self._hide_action()
            if pending:
                await self._run_pending(self.session.cancel, pending.pending_id)
        elif button_id == "key-cancel":
            pending = self._pending_response
            self._hide_key()
            self._pending_response = None
            await asyncio.to_thread(self.session.cancel, pending.pending_id if pending else None)
            self._message(self._say("Canceled: no API key was provided.", "已取消：未提供 API 密钥。"))
        elif button_id == "help-close":
            self.post_message(HelpCloseRequested())

    async def _run_pending(self, method, pending_id: str | None) -> None:
        self._busy = True
        self._set_busy(True)
        try:
            response = await asyncio.to_thread(method, pending_id)
            await self._render(response)
        except Exception as exc:  # noqa: BLE001
            self._message(self._say("Action failed: ", "操作失败：") + f"{type(exc).__name__}: {exc}")
        finally:
            self._busy = False
            self._set_busy(False)
            self._refresh_task_view()
            self._refresh_status()

    async def _execute_current_plan(self) -> None:
        task_id, shown_hash = self._shown_task, self._shown_hash
        if not task_id or not shown_hash:
            return
        self._busy = True
        self._set_busy(True)
        try:
            state = self.session.tasks.get(task_id)
            if (state["status"] != "awaiting_approval" or state.get("plan_hash") != shown_hash or
                    self.session.active_task != task_id):
                self._message(self._say("The plan changed. Review its current version before executing.", "计划已变化，请检查最新版本后再执行。"))
                return
            self.session.tasks.plan(task_id)
            pending = await asyncio.to_thread(self.session.handle, f"/approve {task_id}")
            current = self.session.tasks.get(task_id)
            if (pending.kind != "need_confirm" or pending.action_id != "task_plan" or
                    current["status"] != "awaiting_approval" or current.get("plan_hash") != shown_hash):
                self.session.cancel(pending.pending_id)
                self._message(self._say("The plan is no longer ready. Review it before executing.", "计划尚未就绪，请重新检查后再执行。"))
                return
            response = await asyncio.to_thread(self.session.confirm, pending.pending_id)
            await self._render(response)
        except (OSError, ValueError) as exc:
            self._message(self._say("Could not execute the displayed plan: ", "无法执行当前计划：") + str(exc))
        finally:
            self._busy = False
            self._set_busy(False)
            self._refresh_task_view()
            self._refresh_status()

    async def _provide_key(self, key: str) -> None:
        pending = self._pending_response
        self._hide_key()
        self._pending_response = None
        if not key:
            await asyncio.to_thread(self.session.cancel, pending.pending_id if pending else None)
            self._message(self._say("Canceled: no API key was provided.", "已取消：未提供 API 密钥。"))
            return
        self._busy = True
        self._set_busy(True)
        try:
            response = await asyncio.to_thread(
                self.session.provide_key, key, pending.pending_id if pending else None)
            await self._render(response)
        except Exception as exc:  # noqa: BLE001
            self._message(self._say("Key handling failed: ", "密钥处理失败：") + f"{type(exc).__name__}: {exc}")
        finally:
            self._busy = False
            self._set_busy(False)
            self._refresh_status()

    def on_click(self, event: Click) -> None:
        """Home example rows fill the input when clicked (never submit)."""
        widget = getattr(event, "widget", None)
        container = self._get("#home-examples")
        if container is None or widget is None or container not in widget.ancestors:
            return
        try:
            line = str(widget.renderable)
        except Exception:  # noqa: BLE001 - plain click, never break the UI
            return
        for text in HOME_EXAMPLES:
            if text in line:
                self.action_example(text)
                return

    def action_refresh_all(self) -> None:
        if self.is_running:
            self._refresh_task_view()
            self._follow_job()
            self._refresh_status()

    def action_show_help(self) -> None:
        if self.is_running:
            self._toggle_help(True)

    def action_close_help(self) -> None:
        if self.is_running:
            self._toggle_help(False)

    def on_help_close_requested(self, event: HelpCloseRequested) -> None:
        event.stop()
        self._toggle_help(False)

    def action_quit_app(self) -> None:
        if self._tick_timer is not None:
            self._tick_timer.stop()
        active = self.session.jobs.active_pipeline()
        if active is not None:
            self._write_chat(f"[yellow]Job {active['job_id']} will continue after exit.[/yellow]")
        self.exit()

"""Slash-command catalogue and prefix matching shared by completion and help.

The catalogue is the single source of truth: the completion menu, the Tab
suggester, and the tests all read it, so a new command cannot appear in one place
and stay invisible in another.
"""
from __future__ import annotations

from dataclasses import dataclass

from rich.text import Text
from textual.containers import Vertical
from textual.widgets import Static

from .controller import READONLY_COMMANDS


@dataclass(frozen=True)
class Command:
    """One slash command; the hint shows argument shape without accepting input."""

    name: str
    english: str
    chinese: str

    @property
    def read_only(self) -> bool:
        return self.name in READONLY_COMMANDS


# Read-only commands come first so they win the prefix race while a request runs.
COMMANDS: tuple[Command, ...] = tuple(
    [Command(name, *hint) for name, *hint in (
        ("/help", "Commands and keys", "命令与快捷键"),
        ("/status", "Run status [run-id]", "查看运行状态 [run-id]"),
        ("/runs", "List runs", "列出运行"),
        ("/figures", "List figures", "列出图"),
        ("/report", "Run report [run-id]", "运行报告 [run-id]"),
        ("/jobs", "List background jobs", "列出后台任务"),
        ("/tail", "Job log <job-id> [n]", "任务日志 <job-id> [n]"),
        ("/modules", "List modules", "列出模块"),
        ("/datasets", "List built-in datasets", "列出内置数据集"),
        ("/key", "Enter the API key (masked)", "输入 API 密钥（掩码）"),
        ("/model", "Configure service and model", "设置服务与模型"),
        ("/ask", "Ask with source excerpts <question>", "带来源摘录提问 <问题>"),
        ("/wiki", "Scientific knowledge and review", "科研知识检索与审查"),
        ("/new", "Start a separate task", "开始独立任务"),
        ("/cd", "Workspace [directory]", "工作区 [目录]"),
        ("/session", "Open a task <task-id>", "打开任务 <task-id>"),
        ("/sessions", "List saved tasks", "列出任务会话"),
        ("/approve", "Re-confirm a plan [task-id]", "重新确认计划 [task-id]"),
        ("/retry", "Plan again [task-id]", "重新规划 [task-id]"),
        ("/cancel", "Cancel the pending confirmation", "取消待确认操作"),
        ("/quit", "Exit the interface", "退出界面"),
        ("/run", "Full pipeline [run-id]", "完整流程 [run-id]"),
        ("/data", "Validate inputs [run-id]", "校验输入 [run-id]"),
        ("/resume", "Resume a run <run-id>", "继续运行 <run-id>"),
        ("/research", "Continue research [task-id]", "继续科研解释 [task-id]"),
        ("/verify", "Verify a run [run-id]", "验证运行 [run-id]"),
        ("/dossiers", "Publish dossiers [run-id]", "发布靶点档案 [run-id]"),
        ("/verify-dossiers", "Verify dossiers [run-id]", "验证档案 [run-id]"),
        ("/module", "Run a module <name>", "运行模块 <名称>"),
        ("/kill", "Stop a job <job-id>", "停止任务 <job-id>"),
    )]
)
COMMAND_LIST = [command.name for command in COMMANDS]
_BY_NAME = {command.name: command for command in COMMANDS}


def matches(text: str, *, busy: bool = False, limit: int = 6) -> list[Command]:
    """Prefix matches for a single command line, in catalogue order.

    Anything that is not a lone command prefix (a natural-language request, a
    multiline draft, a command that already has arguments, or a command that is
    already complete) yields no matches, so Enter still submits a typed command.
    """
    value = text
    if value != value.strip() or not value.startswith("/") or len(value.split()) > 1:
        return []
    pool = [item for item in COMMANDS if item.read_only] if busy else list(COMMANDS)
    found = [item for item in pool if item.name.startswith(value) and item.name != value]
    return found[:limit]


def completion(command_name: str) -> str:
    """Text inserted when a command is accepted.

    Exactly the command name, with no trailing space: the menu and the Tab
    suggester must agree, and a second Enter then runs the command instead of
    re-accepting it. Arguments are typed after the name, as in a shell.
    """
    return command_name if command_name in _BY_NAME else ""


class CommandMenu(Vertical):
    """Inline candidate list for slash commands.

    Rows are rebuilt when the candidate set changes; the widget owns only the
    highlight and reports an accepted command through a callback, so it never
    edits the editor itself.
    """

    def __init__(self, on_accept, **kwargs) -> None:
        super().__init__(**kwargs)
        self._on_accept = on_accept
        self._matches: list[Command] = []
        self._index = 0
        self._language = "en"

    def on_mount(self) -> None:
        self.display = False
        self.styles.height = 0

    def visible(self) -> bool:
        return bool(self._matches) and bool(self.display)

    def refresh_for(self, text: str, *, busy: bool, language: str) -> None:
        self._language = language
        found = matches(text, busy=busy)
        if [item.name for item in found] == [item.name for item in self._matches]:
            return  # Same candidate set: keep the highlight where the user left it.
        self._matches = found
        self._index = 0
        self.remove_children()
        if not found:
            self.close()
            return
        for item in found:
            self.mount(Static(self._row(item, False), markup=False))
        self.display = True
        self.styles.height = len(found)
        self._paint()

    def move(self, step: int) -> bool:
        if not self.visible():
            return False
        self._index = (self._index + step) % len(self._matches)
        self._paint()
        return True

    def accept(self) -> bool:
        if not self.visible():
            return False
        chosen = completion(self._matches[self._index].name)
        self.close()
        self._on_accept(chosen)
        return True

    def close(self) -> None:
        self._matches = []
        self._index = 0
        self.display = False
        self.styles.height = 0

    def _row(self, item: Command, highlighted: bool) -> Text:
        zh = self._language == "zh"
        line = Text("❯ " if highlighted else "  ", style="#4a9eff")
        line.append(item.name + " " * max(1, 18 - len(item.name)),
                    style="#ffffff" if highlighted else "#d4d4d4")
        line.append(item.chinese if zh else item.english, style="#8b9bb0")
        return line

    def _paint(self) -> None:
        """Mark the highlighted row without rebuilding the whole list."""
        rows = [child for child in self.children if isinstance(child, Static)]
        for position, row in enumerate(rows):
            if position < len(self._matches):
                row.update(self._row(self._matches[position], position == self._index))

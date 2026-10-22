"""Multiline request editor with explicit submission and lossless paste."""
from __future__ import annotations

from textual import events
from textual.message import Message
from textual.widgets import TextArea


class PromptEditor(TextArea):
    """Enter submits; Ctrl+J inserts a newline. Paste never submits.

    A completion menu may own Up/Down, Tab and Enter while it is open; the editor
    only forwards those keys when the menu declines them, so a fully typed command
    still submits on the first Enter.
    """

    class Submitted(Message):
        def __init__(self, editor: "PromptEditor") -> None:
            super().__init__()
            self.editor = editor
            self.value = editor.text

    def __init__(self, *, suggester=None, submit_handler=None, menu=None, **kwargs):
        super().__init__(soft_wrap=True, tab_behavior="focus", **kwargs)
        self.suggester = suggester
        self.submit_handler = submit_handler
        self.menu = menu

    @property
    def value(self) -> str:
        return self.text

    @value.setter
    def value(self, text: str) -> None:
        self.load_text(text)
        self.move_cursor(self.document.end)

    def submit(self) -> None:
        if self.submit_handler is not None:
            # Capture and clear synchronously, before the next queued character.
            self.submit_handler(self)
        else:
            self.post_message(self.Submitted(self))

    def _menu_owns_key(self, event: events.Key) -> bool:
        """Return True when the completion menu consumed this key."""
        menu = self.menu
        if menu is None or not menu.visible():
            return False
        # The menu describes a single command line, so cursor movement is irrelevant here.
        if "\n" in self.text or not self.selection.is_empty:
            return False
        if event.key in {"up", "down"}:
            return menu.move(-1 if event.key == "up" else 1)
        if event.key in {"tab", "enter"}:
            return menu.accept()
        return False

    async def _on_key(self, event: events.Key) -> None:
        if self._menu_owns_key(event):
            event.stop()
            event.prevent_default()
            return
        if event.key in {"enter", "ctrl+j"}:
            event.stop()
            event.prevent_default()
            if event.key == "enter":
                self.submit()
            elif not self.read_only:
                self._replace_via_keyboard("\n", *self.selection)
            return
        if (event.key in {"tab", "right"} and self.suggester is not None
                and self.text.startswith("/") and "\n" not in self.text
                and self.selection.is_empty and self.cursor_location == self.document.end):
            suggestion = await self.suggester.get_suggestion(self.text)
            if suggestion and suggestion != self.text:
                event.stop()
                event.prevent_default()
                self.value = suggestion
                return
        # Textual dispatches unhandled events to the base class automatically.

    async def _on_paste(self, event: events.Paste) -> None:
        # Stop bubbling explicitly: one paste replaces one selection once.
        event.stop()
        event.prevent_default()
        await super()._on_paste(event)

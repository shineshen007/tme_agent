"""Multiline request editor with explicit submission and lossless paste."""
from __future__ import annotations

from textual import events
from textual.message import Message
from textual.widgets import TextArea


class PromptEditor(TextArea):
    """Enter submits; Ctrl+J inserts a newline. Paste never submits."""

    class Submitted(Message):
        def __init__(self, editor: "PromptEditor") -> None:
            super().__init__()
            self.editor = editor
            self.value = editor.text

    def __init__(self, *, suggester=None, submit_handler=None, **kwargs):
        super().__init__(soft_wrap=True, tab_behavior="focus", **kwargs)
        self.suggester = suggester
        self.submit_handler = submit_handler

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

    async def _on_key(self, event: events.Key) -> None:
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

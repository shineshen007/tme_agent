"""Standard-library REPL fallback used when textual is unavailable (or --repl)."""
from __future__ import annotations

import getpass
import sys

from .controller import Response, Session
from .root import resolve_root

BANNER = """multiomics REPL
- Enter an English or Chinese request, or a / command. Use /help or /quit.
- For the full-screen interface, install textual: .venv/bin/pip install "textual>=1,<2"
"""


def _out(text: str = "") -> None:
    print(text)


def _show_excerpts(response: Response) -> None:
    if response.answer:
        label = "AI summary; review against sources" if response.mode == "llm" else "Source retrieval"
        _out(f"[multiomics · {label}]")
        _out(response.answer)
    if response.notice:
        _out(f"Notice: {response.notice}")
    for excerpt in response.excerpts:
        location = f"{excerpt['source']}:{excerpt['line']}" if excerpt.get("line") else excerpt["source"]
        _out("")
        _out(f"[{excerpt['id']}] {location}")
        if excerpt.get("heading"):
            _out(excerpt["heading"])
        _out(excerpt["text"])


def _handle(session: Session, response: Response) -> bool:
    """Render a response, following confirmation/key prompts. False ends the loop."""
    while True:
        if response.kind == "quit":
            _out("Goodbye.")
            return False
        if response.kind == "need_confirm":
            _out(response.message)
            try:
                answer = input("Confirm? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                _out("")
                session.cancel()
                return False
            response = session.confirm(response.pending_id) if answer in {"y", "yes", "是"} else session.cancel(response.pending_id)
            continue
        if response.kind == "need_key":
            _out(response.message)
            try:
                key = getpass.getpass("Current endpoint API key (hidden; memory only): ").strip()
            except (EOFError, KeyboardInterrupt):
                session.cancel(response.pending_id)
                return True
            if not key:
                _out("Canceled: no API key was provided.")
                session.cancel(response.pending_id)
                return True
            response = session.provide_key(key, response.pending_id)
            continue
        if response.kind == "job":
            _out(response.message)
            if response.job:
                _out("Use /tail <job-id> to follow the log, or /jobs to list jobs.")
            return True
        if response.kind == 'model_settings':
            _out('请在全屏界面使用 /model；REPL 可编辑 config/api.local.yaml 后重新启动。密钥仍通过 /key 输入。')
            return True
        if response.kind == "excerpts":
            _show_excerpts(response)
            return True
        if response.message:
            _out(response.message)
        return True


def run_repl(session: Session) -> int:
    _out(BANNER)
    while True:
        try:
            text = input("multiomics> ").strip()
        except (EOFError, KeyboardInterrupt):
            _out("")
            return 0
        if not text:
            continue
        try:
            response = session.handle(text)
        except Exception as exc:  # noqa: BLE001 - REPL must never crash
            _out(f"Internal error: {type(exc).__name__}: {exc}")
            continue
        if not _handle(session, response):
            return 0


if __name__ == "__main__":
    sys.exit(run_repl(Session(resolve_root())))

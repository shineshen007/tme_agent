"""Framework-agnostic TUI session controller shared by the Textual app and the REPL.

This module is where governance is enforced for interactive actions: only
whitelisted actions can start subprocesses, mutating actions require explicit
confirmation, key material stays in memory, and Q&A stays retrieval-first.
"""
from __future__ import annotations

import os
import re
import sys
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import artifacts, qa as qa_module
from .actions import (ActionError, SPECS, build_command, display_job_label, list_modules, new_run_id)
from .jobs import JobManager, display_warning
from .llm import build_client, key_env_name, key_present, set_session_key
from .task_sessions import TaskStore, redact_secrets
from .task_planner import PlanningContextError, classify, generate_plan, render_plan
from orchestrator.llm_client import LLMConfigError, LLMTransportError, LLMValidationError


def planning_failure_message(kind: str | None, language: str) -> str:
    from .language import choose
    messages = {
        "request_size": ("Full request exceeds the model context; original text is preserved. Shorten the request or choose a model with a larger context.",
                         "完整要求超出模型上下文，原文已保留；请精简要求或选择更大上下文的模型。"),
        "validation": ("Model reply failed validation; use /retry to plan again.",
                       "模型回复校验失败，可使用 /retry 重试。"),
        "connection": ("Model connection failed; check /model, then use /retry.",
                       "模型连接失败，请检查 /model 后使用 /retry 重试。"),
        "configuration": ("Model configuration unavailable; check /model or /key, then use /retry.",
                          "模型配置不可用，请检查 /model 或 /key 后使用 /retry 重试。"),
    }
    en, zh = messages.get(kind, ("Planning failed; inspect Details, then use /retry.",
                                 "计划生成失败，请查看 Details，可使用 /retry 重试。"))
    return choose(language, en, zh)

HELP_TEXT = """multiomics quick reference

Start a task
  Type a request in English or Chinese; review the plan before it runs.
  /new               Start a separate task
  /key               Enter the API key in a masked prompt (memory only)
  /model             Configure DeepSeek, compatible API or local model

Run and inspect
  /data [run-id]     Validate inputs and register samples (confirm)
  /run [run-id]      Full pipeline (key and confirm required)
  /resume <run-id>   Resume a matching run (key and confirm required)
  /verify [run-id]   Verify a run (read-only)
  /dossiers [run-id] Publish target dossiers (confirm)
  /module <name>     Run a module (confirm); /modules lists modules
  /jobs              List jobs; /tail <job-id> [n] shows log lines
  /kill <job-id>     Stop a job (confirm)

Explore and sessions
  /status [run-id]   /runs   /figures   /report [run-id]
  /ask <question>    Source excerpts; add --summary for an AI summary
  /sessions          /session <task-id>   /approve [task-id]   /cancel
  /retry [task-id]    Retry planning for a draft or blocked task

Keys
  Enter Submit  F1 Help / Esc Close  Ctrl+R Refresh  Ctrl+Q Quit
  While a request runs only /help /status /runs /figures /jobs /tail /modules.
  Jobs continue after exit. Plans, logs and citations stay verbatim.
"""

COMMAND_INTENTS = {
    "/run": ("run_all", "run_id"), "/data": ("run_data", "run_id"),
    "/resume": ("resume", "run_id"), "/verify": ("verify", "run_id"),
    "/dossiers": ("dossiers", "run_id"), "/verify-dossiers": ("verify_dossiers", "run_id"),
    "/module": ("module_run", "module"), "/modules": ("module_list", None),
    "/status": ("status", "run_id"), "/runs": ("list_runs", None),
    "/figures": ("list_figures", None), "/report": ("show_report", "run_id"),
    "/ask": ("qa", "question"), "/quit": ("quit", None), "/help": ("help", None),
}


@dataclass
class Response:
    kind: str
    message: str = ""
    action_id: str | None = None
    slots: dict = field(default_factory=dict)
    argv: list | None = None
    job: dict | None = None
    answer: str | None = None
    mode: str | None = None
    excerpts: list = field(default_factory=list)
    notice: str | None = None
    pending_id: str | None = None
    read_only: bool = False


# Commands safe to run while a planning request or job is in flight: they only
# read framework state and never touch pending confirmations or task state.
READONLY_COMMANDS = ("/help", "/status", "/runs", "/figures", "/jobs", "/tail", "/modules")


def _tail_lines(args: list) -> int:
    return int(args[1]) if len(args) > 1 and args[1].isdigit() else 200


class Session:
    def __init__(self, root, cfg=None, allow_llm: bool = True, client=None):
        self.root = Path(root)
        if cfg is None:
            from .llm import load_tui_config
            cfg = load_tui_config(self.root)
        self.cfg = cfg
        self.allow_llm = allow_llm
        self.jobs = JobManager(self.root)
        self.tasks = TaskStore(self.root)
        saved_tasks = self.tasks.list()
        self.active_task = saved_tasks[0]["task_id"] if saved_tasks else None
        self.pending: dict | None = None
        self._pending_lock = threading.RLock()
        self.client = client
        self._model_checked = None
        if self.client is None:
            self.refresh_client()

    # ---------- LLM ----------
    def refresh_client(self) -> None:
        if not self.allow_llm or not key_present(self.cfg):
            self.client = None
            return
        try:
            self.client = build_client(self.root, self.cfg, artifacts.registry_ids(self.root))
        except Exception:  # noqa: BLE001 - LLM is optional
            self.client = None

    @property
    def llm_mode(self) -> str:
        return "LLM" if self.allow_llm and self.client is not None else "Keywords"

    def key_available(self) -> bool:
        return key_present(self.cfg)

    def help_text(self):
        from .credentials import enabled
        if enabled(self.cfg):
            return HELP_TEXT.replace('(memory only)', '(saved in OS credential vault)') + '\n  /key delete        Delete this endpoint\'s saved key\n'
        return HELP_TEXT

    def set_key(self, key: str) -> None:
        self._model_checked = None
        set_session_key(self.cfg, key)
        self.refresh_client()

    def connection_tested(self):
        from .model_config import public_settings
        try:
            return self._model_checked == public_settings(self.cfg)
        except ValueError:
            return False

    def save_model(self, settings, key=''):
        from .model_config import overlay, save_settings, ready
        if getattr(self, 'model_busy', False) or self.jobs.active_pipeline() is not None:
            raise ValueError('当前请求或后台任务仍在执行，完成后才能保存模型设置。')
        value = overlay(self.cfg['llm'], settings)
        save_settings(self.root, value)
        self.cfg = {**self.cfg, 'llm': value}
        if key.strip():
            self.set_key(key)
        else:
            self.refresh_client()
        status = '模型设置已保存；连接检查通过。' if self.connection_tested() else '模型设置已保存；尚未测试连接。'
        return status + ('' if ready(self.cfg) else ' 请通过 /key 输入此接口的密钥。')

    def test_model(self, settings, key=''):
        from .model_config import overlay, store_key, ready
        from .model_client import test_connection
        cfg = {**self.cfg, 'llm': overlay(self.cfg['llm'], settings)}
        if key.strip():
            store_key(cfg, key)
        if not ready(cfg):
            return {'status': 'failed', 'message': '认证未配置：请在掩码输入框填写此接口的密钥。'}
        result = test_connection(self.root, cfg)
        from .model_config import public_settings
        self._model_checked = public_settings(cfg) if result['status'] == 'passed' else None
        return result

    # ---------- dispatch ----------
    def handle(self, text: str) -> Response:
        text = (text or "").strip()
        self.cancel()  # a new request invalidates any earlier confirmation
        if not text:
            return Response("text", "Enter a request or use /help to see commands.")
        if redact_secrets(text) != text:
            return Response("text", "A credential was detected. Use /key to enter it in the masked prompt.")
        if text.startswith("/"):
            response = self.handle_command(text)
            if self.active_task and text.split()[0].lower() not in {"/key", "/model", "/new", "/session", "/sessions", "/retry"}:
                self.tasks.event(self.active_task, "user", text)
                self.tasks.event(self.active_task, "assistant", response.message or response.answer or response.kind)
            return response
        return self.handle_natural_task(text)

    def reply_language(self) -> str:
        if self.active_task:
            return self.tasks.reply_language(self.active_task)
        return getattr(self, "_reply_language", "en")

    def handle_tui(self, text: str, expected_hash: str | None = None) -> Response:
        """Explicit TUI contract: every ordinary request is planned before execution."""
        from .language import detect_language, choose
        text = text or ""
        language = detect_language(text, self.reply_language())
        self._reply_language = language
        if not text.strip() or redact_secrets(text) != text:
            return Response("text", choose(language, "Enter a request; use /key for credentials.",
                                           "请输入任务；密钥请通过 /key 输入。"))
        if text.lstrip().startswith("/"):
            text = text.strip()
            if text.startswith("/ask ") and self.active_task:
                self.tasks.set_reply_language(self.active_task, language)
            return self.handle(text)
        state = self.tasks.get(self.active_task) if self.active_task else None
        if state and state["status"] in {"approved", "generating_code", "running", "verifying"}:
            return Response("text", choose(language,
                "This task is running. Wait for completion or use /new for a separate task.",
                "当前任务正在执行。请等待完成，或使用 /new 创建独立任务。"))
        if state and state["status"] in {"draft", "blocked", "needs_clarification", "awaiting_approval"}:
            self.tasks.set_reply_language(state["task_id"], language)
            if not state["goal"]:
                state["goal"] = text
                self.tasks.save(state)
            return self.revise_plan(text, state["task_id"], expected_hash)
        self.cancel()
        state = self.tasks.create(text)
        self.active_task = state["task_id"]
        self.tasks.set_reply_language(state["task_id"], language)
        return self._plan_for_task(state)

    def handle_natural_task(self, text: str) -> Response:
        """LLM intent first; failed planning never becomes an executable route."""
        if redact_secrets(text) != text:
            return Response("text", "A credential was detected. Use /key to enter it in the masked prompt.")
        from .nl import ACTION_INTENTS, READONLY_RE, keyword_route
        if not self.allow_llm or self.client is None:
            local = keyword_route(text, self.root)
            is_analysis = local.intent in ACTION_INTENTS or re.search(
                r"分析|计算|绘制|画图|生成|运行|\b(?:analy[sz]e|compute|plot|run|generate)\b", text, re.I)
            if is_analysis:
                state = self.tasks.create(text)
                self.active_task = state["task_id"]
                state.update(status="blocked", error="LLM is unavailable")
                self.tasks.save(state)
                message = f"Task {state['task_id']} saved as a draft. LLM is unavailable; no plan or job was started. Set /key and use /session to continue."
                self.tasks.event(state["task_id"], "assistant", message)
                return Response("text", message)
            response = self.dispatch_intent(local.intent, local.slots)
            self._record_query(text, response)
            return response
        current = None
        if self.active_task:
            try:
                current = self.tasks.get(self.active_task)
            except (OSError, ValueError):
                self.active_task = None
        try:
            intent = classify(self.client, text, current["goal"] if current else "")
        except Exception as exc:
            if READONLY_RE.search(text):
                local = keyword_route(text, self.root)
                response = self.dispatch_intent(local.intent, local.slots)
                self._record_query(text, response)
                return response
            # Classification failed, so ownership of this request is unknown.
            # Preserve the previous task and save this request as a separate draft.
            state = self.tasks.create(text)
            self.active_task = state["task_id"]
            state.update(status="blocked", error=f"LLM intent failed: {type(exc).__name__}: {exc}")
            self.tasks.save(state)
            self.tasks.event(state["task_id"], "assistant", "LLM intent failed; no plan or job was started.")
            return Response("text", f"Task {state['task_id']} saved. LLM intent failed; no job was started: {exc}")
        if intent == "ambiguous":
            return Response("text", "Is this a new task or a revision of the current task? Use /new for a new task, or /session <task-id> to revise one.")
        if intent == "question":
            local = keyword_route(text, self.root)
            response = (self.dispatch_intent(local.intent, local.slots) if local.intent in
                        {"module_list", "status", "list_runs", "list_figures", "show_report", "help"}
                        else self.qa_response(text))
            self._record_query(text, response)
            return response
        if intent == "followup" and current and current["status"] in {"draft", "blocked", "needs_clarification", "awaiting_approval"}:
            state = current
            self.tasks.event(state["task_id"], "user", text)
        elif intent == "followup":
            return Response("text", "No editable task is selected. Use /new or select a draft with /session <task-id>.")
        elif current and current["status"] == "draft" and not current["goal"]:
            state = current
            state["goal"] = text
            self.tasks.save(state)
            self.tasks.event(state["task_id"], "user", text)
        else:
            state = self.tasks.create(text)
            self.active_task = state["task_id"]
        task_id = state["task_id"]
        if state["status"] == "awaiting_approval":
            state.update(status="draft", approved_hash=None)
            self.tasks.save(state)
        return self._plan_for_task(state)

    def revise_plan(self, text: str, task_id: str, expected_hash: str | None = None) -> Response:
        """Revise the selected unapproved task without intent classification."""
        text = (text or "").strip()
        if not text:
            return Response("text", "Describe the clarification or plan change.")
        if redact_secrets(text) != text:
            return Response("text", "A credential was detected. Use /key to enter it in the masked prompt.")
        self.cancel()
        try:
            state = self.tasks.get(task_id)
        except (OSError, ValueError):
            return Response("text", f"Unknown task session: {task_id}")
        if state["status"] not in {"draft", "blocked", "needs_clarification", "awaiting_approval"}:
            return Response("text", f"Task {task_id} is {state['status']}; start a new task to change the plan.")
        if expected_hash is not None and state.get("plan_hash") != expected_hash:
            return Response("text", "The plan changed. Review the current version before editing.")
        self.active_task = task_id
        self.tasks.event(task_id, "user", text)
        if state["status"] == "awaiting_approval":
            state.update(status="draft", approved_hash=None)
            self.tasks.save(state)
        return self._plan_for_task(state)

    def _plan_for_task(self, state: dict) -> Response:
        task_id = state["task_id"]
        request = self.tasks.user_text(task_id)
        from .language import choose
        language = self.tasks.reply_language(task_id)
        try:
            if not self.allow_llm or self.client is None:
                raise LLMConfigError(choose(language, "LLM unavailable. Check /model or /key, then use /retry.",
                                           "LLM 不可用。请检查 /model 或 /key 后使用 /retry 重试。"))
            plan = generate_plan(self.client, self.root, request, self.cfg, language=language)
        except Exception as exc:
            state = self.tasks.get(task_id)
            kind = ("request_size" if isinstance(exc, PlanningContextError) else
                    "configuration" if isinstance(exc, LLMConfigError) else
                    "connection" if isinstance(exc, (LLMTransportError, ConnectionError, TimeoutError)) else
                    "validation" if isinstance(exc, (LLMValidationError, ValueError)) else "internal")
            state.update(status="blocked", error=redact_secrets(f"Planning failed: {type(exc).__name__}: {exc}"),
                         planning_failure_kind=kind, questions=[], approved_hash=None)
            self.tasks.save(state)
            message = choose(language, "Planning failed; no job started. ", "计划生成失败，尚未执行任务。") + planning_failure_message(kind, language)
            self.tasks.event(task_id, "assistant", message)
            return Response("text", message)
        if "questions" in plan:
            state = self.tasks.get(task_id)
            state.update(status="needs_clarification", error=None, planning_failure_kind=None,
                         approved_hash=None, questions=plan["questions"])
            self.tasks.save(state)
            message = choose(language, "Please clarify:\n", "请补充说明：\n") + "\n".join(plan["questions"])
            self.tasks.event(task_id, "assistant", message)
            return Response("text", message)
        state = self.tasks.save_plan(task_id, plan)
        message = render_plan(task_id, plan, state["plan_hash"], language=self.tasks.reply_language(task_id))
        self.tasks.event(task_id, "assistant", message)
        with self._pending_lock:
            self.pending = {"id": uuid.uuid4().hex, "action": "task_plan", "task_id": task_id,
                            "plan_hash": state["plan_hash"], "ready": True}
            return Response("need_confirm", message, action_id="task_plan", pending_id=self.pending["id"])

    def _record_query(self, text: str, response: Response) -> None:
        if self.active_task:
            self.tasks.event(self.active_task, "user", text)
            self.tasks.event(self.active_task, "assistant", response.message or response.answer or response.kind)
        else:
            import json
            path = self.root / "workspace/tui/query_history.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(json.dumps({"request": redact_secrets(text),
                                         "response": redact_secrets(response.message or response.answer or response.kind)}, ensure_ascii=False) + "\n")

    def process_view_command(self, text: str, *, read_only: bool = False) -> Response:
        """Read-only commands. ``read_only=True`` marks them for the busy-state UI path."""
        parts = text.split()
        command, args = parts[0].lower(), parts[1:]
        if command == "/help":
            response = Response("text", self.help_text())
        elif command == "/jobs":
            response = self.jobs_response()
        elif command == "/tail":
            if not args:
                response = Response("text", "Usage: /tail <job-id> [lines]")
            else:
                response = Response("text", self.jobs.tail(args[0], _tail_lines(args)))
        elif command == "/modules":
            response = self.modules_response()
        elif command in {"/status", "/runs", "/figures"}:
            intent, slot_name = COMMAND_INTENTS[command]
            slots = {slot_name: args[0]} if slot_name and args else {}
            response = self.dispatch_intent(intent, slots)
        else:
            response = Response("text", f"Unknown command {command}. Use /help to see supported commands.")
        if read_only:
            response.read_only = True
        return response

    def readonly_command(self, text: str) -> Response:
        """Entry point for the TUI busy state; refuses anything not on the whitelist."""
        command = (text or "").strip().split()
        if not command or command[0].lower() not in READONLY_COMMANDS:
            return Response("text", "This command is not allowed while a request is running.")
        return self.process_view_command(text, read_only=True)

    def handle_command(self, text: str) -> Response:
        parts = text.split()
        command, args = parts[0].lower(), parts[1:]
        if command in READONLY_COMMANDS:
            return self.process_view_command(text)
        if command == "/new":
            state = self.tasks.create()
            self.active_task = state["task_id"]
            return Response("text", f"New task {state['task_id']}. Describe the analysis goal in natural language.")
        if command == "/sessions":
            states = [self.tasks.reconcile(item["task_id"], self.jobs) for item in self.tasks.list()]
            if not states:
                return Response("text", "No task sessions found.")
            return Response("text", "Task sessions:\n" + "\n".join(
                f"{item['task_id']}  {item['status']}  {item['goal'][:75]}" for item in states[:50]))
        if command == "/session":
            if not args:
                return Response("text", "Usage: /session <task-id>")
            try:
                state = self.tasks.reconcile(args[0], self.jobs)
            except (OSError, ValueError):
                return Response("text", f"Unknown task session: {args[0]}")
            self.active_task = state["task_id"]
            return Response("session", f"Task {state['task_id']} ({state['status']})\n\n" +
                            self.tasks.transcript(state["task_id"]))
        if command == "/retry":
            from .language import choose
            language = self.reply_language()
            if len(args) > 1:
                return Response("text", "Usage: /retry [task-id]")
            task_id = args[0] if args else self.active_task
            if not task_id:
                return Response("text", choose(language, "Select a task with /session <task-id> first.",
                                               "请先使用 /session <task-id> 选择任务。"))
            try:
                state = self.tasks.get(task_id)
            except (OSError, ValueError):
                return Response("text", choose(language, "Unknown task session: ", "未知任务会话：") + task_id)
            language = self.tasks.reply_language(task_id)
            if state["status"] not in {"draft", "blocked"}:
                return Response("text", choose(language,
                    "Only draft or blocked tasks can retry planning; answer pending questions or review the current plan.",
                    "仅 draft 或 blocked 任务可重试规划；请回答待澄清问题或查看当前计划。"))
            if not self.tasks.user_text(task_id).strip():
                return Response("text", choose(language, "Describe the task first.", "请先输入任务要求。"))
            self.cancel()
            self.active_task = task_id
            self.tasks.event(task_id, "system", "Retry planning with the existing user request")
            return self._plan_for_task(state)
        if command == "/approve":
            task_id = args[0] if args else self.active_task
            if not task_id:
                return Response("text", "No selected task; use /session <task-id>.")
            try:
                state = self.tasks.get(task_id)
                if state["status"] != "awaiting_approval":
                    return Response("text", f"Task {task_id} is {state['status']}; no plan awaits approval.")
                plan = self.tasks.plan(task_id)
            except (OSError, ValueError) as exc:
                return Response("text", f"Could not load plan: {exc}")
            self.active_task = task_id
            message = render_plan(task_id, plan, state["plan_hash"], language=self.tasks.reply_language(task_id))
            with self._pending_lock:
                self.pending = {"id": uuid.uuid4().hex, "action": "task_plan", "task_id": task_id,
                                "plan_hash": state["plan_hash"], "ready": True}
                return Response("need_confirm", message, action_id="task_plan", pending_id=self.pending["id"])
        if command == "/key":
            if args == ['delete']:
                from .model_config import forget_key
                if getattr(self, 'model_busy', False) or self.jobs.active_pipeline() is not None:
                    return Response('text', '任务执行中不能删除密钥，请完成或停止后再操作。')
                forget_key(self.cfg)
                self.client = None
                self._model_checked = None
                return Response('text', '已删除此接口保存的密钥；以后使用需重新输入。')
            if args:
                return Response('text', '使用 /key 打开掩码输入框，或 /key delete 删除当前接口密钥。')
            if self.cfg.get('llm', {}).get('auth_mode') == 'none':
                return Response('text', '当前接口使用无认证模式，无需密钥；可通过 /model 更改。')
            from .credentials import enabled
            if enabled(self.cfg):
                return Response('need_key', '输入或替换此接口的密钥，将保存到系统凭据库，重启后自动读取。'
                                + (self.cfg.get('_credential_error') or ''))
            return Response("need_key", "Enter or replace this endpoint's API key; it stays in memory.")
        if command == '/model':
            if getattr(self, 'model_busy', False) or self.jobs.active_pipeline() is not None:
                return Response('text', '当前请求或后台任务仍在执行，完成后才能更改模型设置。')
            return Response('model_settings', 'Configure model connection')
        if command == "/kill":
            if not args:
                return Response("text", "Usage: /kill <job-id>")
            return self.kill_job(args[0])
        if command == "/cancel":
            return self.cancel()
        if command not in COMMAND_INTENTS:
            return Response("text", f"Unknown command {command}. Use /help to see supported commands.")
        intent, slot_name = COMMAND_INTENTS[command]
        slots = {}
        if slot_name == "question":
            slots["summary"] = bool(args and args[0] == "--summary")
            slots["question"] = " ".join(args[1:] if slots["summary"] else args)
        elif slot_name and args:
            slots[slot_name] = args[0]
            if command == "/dossiers" and "--force" in args:
                slots["force"] = True
        return self.dispatch_intent(intent, slots, "")

    def dispatch_intent(self, intent: str, slots: dict, reply: str = "") -> Response:
        try:
            if intent == "clarify":
                return Response("text", reply or "Please split the request or use an explicit / command.")
            if intent == "help":
                return Response("text", self.help_text())
            if intent == "quit":
                return Response("quit", "Goodbye.")
            if intent == "module_list":
                return self.modules_response()
            if intent == "status":
                return Response("text", artifacts.run_summary(self.root, slots.get("run_id")))
            if intent == "list_runs":
                return Response("text", artifacts.runs_table(self.root))
            if intent == "list_figures":
                return Response("text", artifacts.figures_table(self.root))
            if intent == "show_report":
                return self.report_response(slots.get("run_id"))
            if intent == "kill_job":
                return self.kill_job(slots.get("job_id"), slots.get("run_id"))
            if intent == "qa":
                return self.qa_response(slots.get("question") or "", summary=bool(slots.get("summary")))
            if intent in {"verify", "verify_dossiers"}:
                return self.execute(intent, slots)
            if intent == "resume" and not slots.get("run_id"):
                return Response("text", "Resume requires an explicit run-id (for example, /resume hcc_p1_agent_20260915e)."
                                         " No run is selected automatically.")
            if intent in {"run_data", "run_all", "resume", "dossiers", "module_run"}:
                return self.propose(intent, slots, reply)
            return Response("text", f"Unsupported action: {intent}.")
        except ActionError as exc:
            self.cancel()
            return Response("text", f"Action rejected: {exc}")
        except Exception as exc:  # noqa: BLE001 - surface, never crash the UI
            self.cancel()
            return Response("text", f"Action failed: {type(exc).__name__}: {exc}")

    # ---------- actions ----------
    def propose(self, action_id: str, slots: dict, reply: str = "") -> Response:
        self.cancel()
        spec = SPECS[action_id]
        slots = self._resolve_run_id(action_id, slots)
        # Validate and freeze exactly what the user will approve, before requesting a key.
        argv = build_command(self.root, action_id, run_id=slots.get("run_id"),
                             module=slots.get("module"), force=bool(slots.get("force")))
        with self._pending_lock:
            self.pending = {"id": uuid.uuid4().hex, "action": action_id, "slots": slots,
                            "argv": argv, "ready": False}
            return self._confirmation(reply)

    def _confirmation(self, reply="") -> Response:
        pending = self.pending
        action = pending["action"]
        if action == "kill_job":
            message = pending["message"]
        else:
            spec = SPECS[action]
            if spec.requires_key and not self.key_available():
                return Response("need_key", f"{spec.label} requires an API key (kept in memory).",
                                action_id=action, slots=dict(pending["slots"]), pending_id=pending["id"])
            message = (f"{reply or spec.label}\nAction: {spec.label}\n"
                       f"Command: {' '.join(pending['argv'])}\nNote: {spec.note}\nConfirm?")
        pending["ready"] = True
        return Response("need_confirm", message, action_id=action, slots=dict(pending["slots"]),
                        argv=pending.get("argv"), pending_id=pending["id"])

    def provide_key(self, key: str, pending_id=None) -> Response:
        with self._pending_lock:
            if pending_id is not None and (not self.pending or self.pending["id"] != pending_id):
                return Response("text", "The pending action expired. Please submit it again.")
            if not key.strip():
                return self.cancel(pending_id)
            self.set_key(key)
            if pending_id is None:  # standalone /key never resumes a hidden action
                from .credentials import enabled
                if enabled(self.cfg):
                    return Response('text', 'API 密钥已保存到系统凭据库，重启后自动读取。使用 /key delete 可删除。')
                return Response("text", "API key saved in process memory.")
            return self._confirmation()

    def confirm(self, pending_id=None) -> Response:
        with self._pending_lock:
            pending = self.pending
            if not pending or pending["id"] != pending_id or not pending["ready"]:
                return Response("text", "Confirmation expired or is not ready. Nothing was executed.")
            self.pending = None  # consume before performing any side effect
        try:
            if pending["action"] == "task_plan":
                task_id = pending["task_id"]
                active = self.jobs.active_pipeline()
                if active is not None:
                    return Response("text", f"Job {active['job_id']} is active. Task {task_id} still awaits approval; use /approve {task_id} later.")
                self.tasks.approve(task_id, pending["plan_hash"])
                from .model_config import worker_environment
                # Freeze exactly this connection. Keys are never part of the snapshot.
                from .jobs import _atomic_json
                _atomic_json(self.tasks.directory(task_id) / 'model_config.json', self.cfg['llm'])
                model_env = worker_environment(self.cfg)
                job = self.jobs.start("task", [sys.executable, str(Path(__file__).with_name("task_runner.py")),
                                               str(self.root.resolve()), task_id],
                                      label="Approved analysis task", run_id=task_id, env_extra=model_env)
                state = self.tasks.get(task_id)
                state["job_id"] = job["job_id"]
                if job["status"] == "failed" and state["status"] == "approved":
                    state.update(status="failed", error=job.get("error") or "Job could not start")
                self.tasks.save(state)
                return Response("job", f"Task {task_id} submitted. Job ID: {job['job_id']}\nLog: {self.root / job['log_path']}", job=job)
            if pending["action"] == "kill_job":
                job_id = pending["slots"]["job_id"]
                job = self.jobs.kill(job_id)
                return Response("text", f"Stop requested for {job_id} (status: {job['status']}). Check its final state in Jobs.")
            return self._execute(pending["action"], pending["slots"], argv=pending["argv"])
        except Exception as exc:
            return Response("text", f"Action failed: {type(exc).__name__}: {exc}")

    def cancel(self, pending_id=None) -> Response:
        with self._pending_lock:
            previous = self.pending
            if pending_id is None or (self.pending and self.pending["id"] == pending_id):
                self.pending = None
            else:
                previous = None
        if previous and previous.get("action") == "task_plan":
            self.tasks.event(previous["task_id"], "system", "Canceled plan confirmation")
        return Response("text", "Canceled.")

    def _resolve_run_id(self, action_id: str, slots: dict) -> dict:
        slots = dict(slots or {})
        if action_id == "module_run":
            return slots
        if not slots.get("run_id") and action_id in {"verify", "verify_dossiers", "dossiers"}:
            slots["run_id"] = artifacts.newest_run_id(self.root)
        if not slots.get("run_id"):
            slots["run_id"] = new_run_id()
        return slots

    def execute(self, action_id: str, slots: dict) -> Response:
        # Public entry cannot bypass the controller's approval state machine.
        if action_id in SPECS and SPECS[action_id].confirm:
            return self.propose(action_id, slots)
        return self._execute(action_id, slots)

    def _execute(self, action_id: str, slots: dict, argv=None) -> Response:
        spec = SPECS.get(action_id)
        if spec is None:
            return Response("text", f"Unknown action: {action_id}")
        slots = self._resolve_run_id(action_id, slots)
        if spec.requires_key and not self.key_available():
            return self.propose(action_id, slots)
        argv = argv or build_command(self.root, action_id, run_id=slots.get("run_id"),
                                     module=slots.get("module"), force=bool(slots.get("force")))
        active = self.jobs.active_pipeline()
        if active is not None:
            return Response("text", f"Job {active['job_id']} ({display_job_label(active['label'])}) is active. "
                                     f"Use /tail {active['job_id']} or confirm /kill before starting another.")
        env_extra = None
        env_name = key_env_name(self.cfg)
        if spec.requires_key:
            from .model_config import credential
            key = credential(self.cfg)
            if not key:
                raise ActionError('历史固定流程需要密钥；本地免认证模型请使用普通中文指令创建新任务。')
            env_extra = {env_name: key}
        kind = "module" if action_id == "module_run" else "pipeline"
        job = self.jobs.start(kind, argv, label=spec.label, run_id=slots.get("run_id"),
                              env_extra=env_extra)
        run_note = f" (run-id: {slots['run_id']})" if slots.get("run_id") else ""
        return Response("job", f"Job submitted: {spec.label}{run_note}\nStatus: {job['status']}\nJob ID: {job['job_id']}\n"
                               f"Log: {self.root / job['log_path']}", job=job)

    # ---------- readonly views ----------
    def modules_response(self) -> Response:
        modules = list_modules(self.root)
        if not modules:
            return Response("text", "No runnable modules were found under analysis/.")
        return Response("text", "Available modules (analysis/):\n" + "\n".join(f"  {name}" for name in modules))

    def jobs_response(self) -> Response:
        jobs = self.jobs.list_jobs()
        if not jobs:
            return Response("text", "No background jobs found.")
        lines = [f"{'job_id':<22} {'status':<10} {'label':<28} started_at"]
        for job in jobs:
            lines.append(f"{job['job_id']:<22} {job['status']:<10} {display_job_label(job.get('label'))[:28]:<28} "
                         f"{job.get('started_at', '')}")
            if job.get("warning"):
                lines.append(f"  Notice: {display_warning(job['warning'])}")
        return Response("text", "\n".join(lines))

    def report_response(self, run_id=None) -> Response:
        run = artifacts.get_run(self.root, run_id) if run_id else artifacts.latest_run(self.root)
        if run is None:
            return Response("text", "No runs found.")
        for candidate in ("report/summary.md", "report/agent_report.md"):
            path = Path(run["path"]) / candidate
            if path.is_file():
                text = artifacts.read_text(path, limit=6000)
                return Response("text", f"Report: {path}\n\n{text}")
        return Response("text", f"Run {run['run_id']} has no report/summary.md.")

    def qa_response(self, question: str, summary=False) -> Response:
        if not question.strip():
            return Response("text", "Enter a question, for example: /ask What evidence supports the 3MT axis?")
        result = qa_module.answer(self.root, question, client=self.client if self.allow_llm else None, summary=summary, language=self.reply_language())
        message = result.get("answer") or ""
        if result.get("notice"):
            message = (message + "\n\n" if message else "") + f"Notice: {result['notice']}"
        return Response("excerpts", message, answer=result.get("answer"),
                        mode=result.get("mode"), excerpts=result.get("excerpts") or [],
                        notice=result.get("notice"))

    # ---------- jobs ----------
    def kill_job(self, job_id=None, run_id=None) -> Response:
        if not job_id and run_id:
            active = self.jobs.active_pipeline()
            if active and active.get("run_id") == run_id:
                job_id = active["job_id"]
        if not job_id:
            return Response("text", "Enter a job ID; use /jobs to list them.")
        self.cancel()
        job = self.jobs.get(job_id)
        if job is None:
            return Response("text", f"Unknown job: {job_id}")
        message = (f"Stop job: {job_id}\nRun ID: {job.get('run_id') or 'none'}\n"
                   f"Action: {display_job_label(job.get('label')) or 'unknown'}\n"
                   "Send SIGTERM to the process group; use SIGKILL after 10 seconds if needed, "
                   "then wait up to 5 seconds. The job slot stays occupied until exit. Confirm stop?")
        with self._pending_lock:
            self.pending = {"id": uuid.uuid4().hex, "action": "kill_job", "slots": {"job_id": job_id},
                            "message": message, "ready": False}
            return self._confirmation()

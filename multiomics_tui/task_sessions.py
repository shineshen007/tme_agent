"""Durable, append-only conversations for one analysis goal per task."""
from __future__ import annotations

import hashlib
from .platform_support import lock_fd, unlock_fd
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .jobs import _atomic_json

TASK_RE = re.compile(r"task_[0-9]{8}_[0-9]{6}_[0-9a-f]{8}")
TERMINAL = {"succeeded", "failed", "killed", "blocked"}


def redact_secrets(value: str) -> str:
    from .model_config import redact
    result = redact(value)
    key = os.environ.get("DEEPSEEK_API_KEY")
    if key:
        result = result.replace(key, "[REDACTED]")
    result = re.sub(r"sk-[A-Za-z0-9_-]{12,}", "[REDACTED]", result)
    result = re.sub(r"(?i)(?:api[_ -]?key|token|password)\s*[:=]\s*\S+", "[REDACTED]", result)
    return result


def utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_hash(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


class TaskStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.base = self.root / "workspace/tui/sessions"

    def directory(self, task_id: str) -> Path:
        if not TASK_RE.fullmatch(task_id):
            raise ValueError("Invalid task ID")
        return self.base / task_id

    def create(self, request: str = "") -> dict:
        task_id = datetime.now(timezone.utc).strftime("task_%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]
        path = self.directory(task_id)
        path.mkdir(parents=True, mode=0o700)
        path.chmod(0o700)
        state = {"task_id": task_id, "status": "draft", "goal": request,
                 "created_at": utc(), "updated_at": utc(), "plan_version": 0,
                 "plan_hash": None, "approved_hash": None, "job_id": None,
                 "result_dir": str(self.root / "analysis" / task_id), "error": None}
        state.update(research_status='not_requested', scientific_status='NOT_EVALUABLE')
        self.save(state)
        if request:
            self.event(task_id, "user", request)
        return state

    def save(self, state: dict) -> None:
        state = dict(state)
        state["updated_at"] = utc()
        path = self.directory(state["task_id"]) / "session.json"
        _atomic_json(path, state)
        path.chmod(0o600)

    def get(self, task_id: str) -> dict:
        path = self.directory(task_id) / "session.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def list(self) -> list[dict]:
        if not self.base.is_dir():
            return []
        states = []
        for path in sorted(self.base.glob("task_*/session.json"), reverse=True):
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
                if TASK_RE.fullmatch(state.get("task_id", "")):
                    states.append(state)
            except (OSError, ValueError):
                continue
        return states

    def event(self, task_id: str, role: str, content: str, **extra) -> None:
        if role not in {"user", "assistant", "system"}:
            raise ValueError("Invalid event role")
        record = {"at": utc(), "role": role, "content": redact_secrets(content), **extra}
        path = self.directory(task_id) / "events.jsonl"
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND | getattr(os, "O_CLOEXEC", 0), 0o600)
        try:
            lock_fd(fd)
            payload = (json.dumps(record, ensure_ascii=False) + "\n").encode()
            offset = 0
            while offset < len(payload):
                written = os.write(fd, payload[offset:])
                if written <= 0:
                    raise OSError("Short session event write")
                offset += written
            os.fsync(fd)
        finally:
            unlock_fd(fd)
            os.close(fd)

    def transcript(self, task_id: str, limit: int = 80) -> str:
        path = self.directory(task_id) / "events.jsonl"
        if not path.is_file():
            return ""
        lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
        records = [json.loads(line) for line in lines]
        return "\n\n".join(f"{item['role']}: {item['content']}" for item in records)

    def user_text(self, task_id: str) -> str:
        path = self.directory(task_id) / "events.jsonl"
        if not path.is_file():
            return ""
        lines = path.read_text(encoding="utf-8").splitlines()
        records = [json.loads(line) for line in lines]
        # Interface actions are never scientific requirements. Legacy releases
        # recorded cancellation as a user event; retain history but omit it here.
        return "\n".join(item["content"] for item in records
                         if item["role"] == "user" and not item["content"].startswith("/")
                         and not item.get("plan_hash")
                         and item["content"] != "Canceled plan confirmation")

    def reply_language(self, task_id: str) -> str:
        from .language import detect_language
        state = self.get(task_id)
        preferences = self.directory(task_id) / "ui.json"
        if preferences.is_file():
            language = json.loads(preferences.read_text(encoding="utf-8")).get("reply_language")
            if language in {"en", "zh"}:
                return language
        if state.get("reply_language") in {"en", "zh"}:
            return state["reply_language"]
        # Infer old sessions without rewriting their files on restore.
        language = "en"
        path = self.directory(task_id) / "events.jsonl"
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                item = json.loads(line)
                if item["role"] == "user" and not item.get("plan_hash"):
                    language = detect_language(item["content"], language)
        return language

    def set_reply_language(self, task_id: str, language: str) -> None:
        # UI preferences must never race the runner's session status updates.
        if language not in {"en", "zh"}:
            raise ValueError("Unsupported reply language")
        _atomic_json(self.directory(task_id) / "ui.json", {"reply_language": language})

    def original_request(self, task_id: str) -> str:
        path = self.directory(task_id) / "events.jsonl"
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                item = json.loads(line)
                if (item["role"] == "user" and not item.get("plan_hash") and
                        not item["content"].startswith("/")):
                    return item["content"]
        return self.get(task_id)["goal"]

    def reconcile(self, task_id: str, jobs) -> dict:
        state = self.get(task_id)
        if state["status"] in {"approved", "generating_code", "running", "verifying"} and state.get("job_id"):
            job = jobs.get(state["job_id"])
            if job and job["status"] in {"failed", "killed", "exited", "stalled"}:
                state.update(status="killed" if job["status"] == "killed" else ("blocked" if job["status"] == "stalled" else "failed"),
                             error=state.get("error") or f"Job {job['status']}; inspect its log")
                self.save(state)
        return state

    def save_plan(self, task_id: str, plan: dict) -> dict:
        state = self.get(task_id)
        if state["status"] in {"approved", "generating_code", "running", "verifying", "succeeded"}:
            raise ValueError("Approved task cannot be revised; start a new task")
        version = int(state["plan_version"]) + 1
        path = self.directory(task_id) / f"plan_v{version:03d}.json"
        if path.exists():
            raise FileExistsError(path)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(plan, stream, ensure_ascii=False, indent=2)
        state.update(status="awaiting_approval", goal=plan["goal"], plan_version=version,
                     plan_hash=canonical_hash(plan), approved_hash=None, error=None,
                     planning_failure_kind=None, questions=[])
        self.save(state)
        return state

    def plan(self, task_id: str) -> dict:
        state = self.get(task_id)
        if not state["plan_version"]:
            raise ValueError("Task has no plan")
        path = self.directory(task_id) / f"plan_v{state['plan_version']:03d}.json"
        plan = json.loads(path.read_text(encoding="utf-8"))
        if canonical_hash(plan) != state["plan_hash"]:
            raise ValueError("Plan hash mismatch")
        return plan

    def approve(self, task_id: str, plan_hash: str) -> dict:
        state = self.get(task_id)
        if state["status"] != "awaiting_approval" or state["plan_hash"] != plan_hash:
            raise ValueError("Plan changed or is not awaiting approval")
        self.plan(task_id)
        state.update(status="approved", approved_hash=plan_hash)
        self.save(state)
        self.event(task_id, "user", "Confirmed approved plan", plan_hash=plan_hash)
        self.event(task_id, "system", "Plan approved", plan_hash=plan_hash)
        return state

"""Natural language routing: DeepSeek JSON router with a deterministic keyword fallback.

The router can only select whitelisted intents; run-id and module slots are validated
against the filesystem. Replies and Q&A summaries pass the project's number guard, so
the model never authors statistics.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .actions import ActionError, list_modules, validate_module, validate_run_id

PROMPT_PATH = Path(__file__).resolve().parent / "prompts/router.md"

INTENTS = {
    "run_data", "run_all", "resume", "verify", "dossiers", "verify_dossiers",
    "module_run", "module_list", "status", "list_runs", "list_figures",
    "show_report", "qa", "help", "quit", "kill_job", "clarify",
}

ACTION_INTENTS = {"run_data", "run_all", "resume", "verify", "dossiers", "verify_dossiers", "module_run"}

RUN_ID_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{2,100}")

MODULE_ALIASES = {
    "go_gsva_gobp2026": ("gobp2026", "go bp 2026", "go:bp 2026", "go bp v2026", "gobp v2026"),
    "go_gsva_optimal_cutoff": ("optimal cutoff", "最优截断", "最佳截断", "cutpoint"),
    "go_gsva_optimal_cutoff/ne_km": ("ne km", "ne_km", "ne 生存图", "神经内分泌生存图"),
    "go_gsva_prognosis": ("gsva prognosis", "gsva 预后", "预后模块", "中位分组"),
}


@dataclass
class Route:
    intent: str
    slots: dict = field(default_factory=dict)
    reply: str = ""
    source: str = "keyword"

    def to_dict(self) -> dict:
        return {"intent": self.intent, "slots": dict(self.slots), "reply": self.reply, "source": self.source}


def _load_prompt(modules) -> str:
    text = PROMPT_PATH.read_text(encoding="utf-8")
    return text.replace("{modules}", ", ".join(modules) if modules else "(none)")


def _extract_run_id(text: str, root) -> str | None:
    runs_dir = Path(root) / "workspace/runs"
    for token in RUN_ID_TOKEN_RE.findall(text or ""):
        if (runs_dir / token).is_dir():
            return token
    return None


def _extract_module(text: str, root) -> str | None:
    low = (text or "").lower()
    modules = list_modules(root)
    for module in sorted(modules, key=len, reverse=True):
        if re.search(r'(?<![A-Za-z0-9_/])' + re.escape(module.lower()) + r'(?![A-Za-z0-9_/])', low):
            return module
    matches = [module for module, keys in MODULE_ALIASES.items()
               if module in modules and any(re.search(r'(?<![A-Za-z0-9_/])' + re.escape(key.lower()) + r'(?![A-Za-z0-9_/])', low) for key in keys)]
    if len(matches) > 1:
        raise ActionError("Module alias is ambiguous; use /module <full-module-name>.")
    return matches[0] if matches else None


READONLY_RE = re.compile(
    r"不要|别|不必|无需|暂不|不(?:要|运行|执行|停止|终止)|勿|禁止|能否|是否|能不能|可不可以|如何|会不会|只看|只读|"
    r"解释|介绍|说明|是什么|为什么|哪些|多少|结果|证据.*[？?吗]|[？?]|吗[。！!]?\s*$|"
    r"\b(?:do not|don't|never|explain|what|why|how|results?)\b", re.I)
EXECUTE_RE = re.compile(r"运行|重跑|跑一下|跑一遍|跑(?:完整|全流程|全部)|启动|开始分析|执行|生成|构建|发布|恢复|接着跑|继续运行|"
                        r"停止|终止|杀掉|\b(?:run|resume|start|generate|build|publish|kill|stop)\b", re.I)
JOB_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_])\d{8}_\d{6}_[0-9a-f]{6,8}(?![A-Za-z0-9_])")
MUTATING = {"run_data", "run_all", "resume", "dossiers", "module_run", "kill_job"}


def _readonly_request(text):
    return bool(READONLY_RE.search(text))


def _qa(text):
    return Route("qa", {"question": text}, "Treating this as a read-only question.")


def _module_candidates(text, root):
    low = text.lower()
    found = [m for m in list_modules(root) if re.search(r'(?<![A-Za-z0-9_/])' + re.escape(m.lower()) + r'(?![A-Za-z0-9_/])', low)]
    # Parent occurrence inside a fully named child is not an additional action.
    return [m for m in found if not any(n.startswith(m + '/') for n in found)]


def keyword_route(text: str, root) -> Route:
    raw = (text or "").strip()
    low = raw.lower()
    if _readonly_request(raw):
        # These exact readonly views remain useful without interpreting an action.
        if any(k in low for k in ("有哪些模块", "模块列表", "列出模块", "list modules", "show modules", "which modules", "what modules")):
            return Route("module_list")
        if any(k in low for k in ("有哪些运行", "运行历史", "运行列表", "list runs", "show runs")):
            return Route("list_runs")
        if low in {"最近结果怎么样", "最近结果怎么样了", "show status", "latest status"}:
            return Route("status")
        if re.search(r"\b(?:list|show(?: me)?)\s+(?:the\s+)?figures\b", low):
            return Route("list_figures")
        if re.search(r"\b(?:show(?: me)?|open)\s+(?:the\s+)?report\b", low):
            return Route("show_report")
        if re.search(r"\b(?:show(?: me)?|check)\s+(?:the\s+)?(?:latest\s+)?(?:run\s+)?status\b", low):
            return Route("status")
        return _qa(raw)
    if any(k in low for k in ("帮助", "help", "怎么用", "使用说明", "你能做什么")):
        return Route("help")
    if low in {"quit", "exit", "退出", "离开", "退出程序", "关闭界面"}:
        return Route("quit")
    if any(k in low for k in ("模块列表", "列出模块", "list modules", "show modules")):
        return Route("module_list")
    comma_actions = re.search(r"[，,。]\s*(?:再|请|并|也)?\s*(?:运行|执行|启动|跑|生成|停止|校验|验证|恢复)", low)
    if comma_actions or len(_module_candidates(raw, root)) > 1 or re.search(r"然后|接着再|同时|并且|以及|(?:并|和|再)(?:运行|跑|生成|停止|校验|验证)|[;；]|\b(?:then|and)\b", low):
        return Route("clarify", reply="Please split multiple actions or use an explicit / command.")
    try:
        module = _extract_module(raw, root)
    except ActionError as exc:
        return Route("clarify", reply=str(exc))
    if not module and ("模块" in raw or "module" in low) and EXECUTE_RE.search(raw):
        return Route("clarify", reply="Could not identify one module; use /modules and then /module <full-name>.")
    run_id = _extract_run_id(raw, root)
    slots = {"run_id": run_id} if run_id else {}
    if any(k in low for k in ("终止任务", "停止任务", "杀掉任务", "停止后台")) or re.search(r"\b(?:kill|stop)\b", low):
        job = JOB_TOKEN_RE.search(raw)
        return Route("kill_job", {"job_id": job.group()} if job else slots)
    if any(k in low for k in ("数据校验", "输入校验", "先校验数据", "检查数据", "检查输入")) or re.search(r"\b(?:validate|check)\s+(?:the\s+)?(?:data|inputs?)\b|\b(?:run|start)\s+(?:a\s+)?data\s+(?:check|validation)\b", low):
        return Route("run_data")
    if re.search(r"恢复|继续运行|接着跑|\bresume\b", low):
        return Route("resume", slots)
    if module and EXECUTE_RE.search(raw):
        return Route("module_run", {"module": module})
    if any(k in low for k in ("校验证据包", "验证证据包", "verify-dossiers", "verify dossiers", "verify target dossiers")):
        return Route("verify_dossiers", slots)
    if any(k in low for k in ("证据包", "dossiers", "靶点包", "靶点证据")) and EXECUTE_RE.search(raw):
        return Route("dossiers", slots)
    if any(k in low for k in ("校验", "verify", "验证一下")):
        return Route("verify", slots)
    if any(k in low for k in ("运行状态", "进度", "状态", "到哪了", "怎么样了", "怎么样", "status", "progress")):
        return Route("status", slots)
    if any(k in low for k in ("运行历史", "运行列表", "历史运行", "list runs")):
        return Route("list_runs")
    if any(k in low for k in ("图产物", "图表", "figures", "列出图")):
        return Route("list_figures")
    if any(k in low for k in ("报告", "report")):
        return Route("show_report", slots)
    if re.search(r"\b(?:run|start)\s+(?:the\s+)?(?:full|complete|entire|all)\s+(?:pipeline|analysis|workflow)\b|\brun\s+all\b", low) or EXECUTE_RE.search(raw):
        return Route("run_all", slots)
    return _qa(raw)


def constrain_route(routed, text, root):
    """Apply the same local execution contract before and after LLM routing."""
    local = keyword_route(text, root)
    if local.intent == "clarify":
        return local
    if _readonly_request(text):
        return local
    if routed.intent in MUTATING:
        # A model may explain wording, but cannot expand the permitted operation.
        if local.intent != routed.intent:
            return local if local.intent not in MUTATING else Route(
                "clarify", reply="The action is ambiguous; use an explicit / command.")
        for name in ('run_id', 'job_id'):
            value = routed.slots.get(name)
            if value and not re.search(r'(?<![A-Za-z0-9_.-])' + re.escape(value) + r'(?![A-Za-z0-9_.-])', text):
                raise ActionError(f'{name} must appear verbatim in the request')
        if routed.intent == 'module_run' and routed.slots.get('module') != local.slots.get('module'):
            return local
        # Preserve explicit targets even if the model omits them.
        routed.slots.update(local.slots)
    if routed.intent == 'qa':
        routed.slots = {'question': text}
    return routed


def validate_route(parsed: dict, root, modules) -> dict:
    """Raise on any non-whitelisted intent or malformed slot."""
    from orchestrator.checkers import validate_no_numbers

    if not isinstance(parsed, dict):
        raise ValueError("Router output must be a JSON object")
    intent = parsed.get("intent")
    if intent not in INTENTS:
        raise ValueError(f"Intent is not allowed: {intent!r}")
    slots = parsed.get("slots") or {}
    if not isinstance(slots, dict):
        raise ValueError("Slots must be an object")
    normalized = {}
    if slots.get("run_id"):
        normalized["run_id"] = validate_run_id(slots["run_id"])
    if slots.get("job_id"):
        value = str(slots["job_id"])
        if not JOB_TOKEN_RE.fullmatch(value):
            raise ValueError("Invalid job ID")
        normalized["job_id"] = value
    if slots.get("module"):
        normalized["module"] = validate_module(root, slots["module"])
    if slots.get("question") is not None:
        question = str(slots["question"])[:4000]
        normalized["question"] = question
    if intent == "qa" and not (normalized.get("question") or "").strip():
        raise ValueError("Q&A request has no question")
    if intent == "module_run" and not normalized.get("module") and len(modules) > 1:
        raise ValueError("Module run has no module")
    if intent == "module_run" and not normalized.get("module") and len(modules) == 1:
        normalized["module"] = modules[0]
    reply = str(parsed.get("reply") or "")
    if reply:
        validate_no_numbers({"reply": reply})
        if re.search(r"[\u3400-\u9fff]", reply):
            reply = ""  # Interface messages stay in English; source excerpts remain verbatim.
        parsed["reply"] = reply
    parsed["slots"] = normalized
    return parsed


def _validate_input_route(parsed, text, root, modules):
    parsed = validate_route(parsed, root, modules)
    for name in ('run_id', 'job_id'):
        value = parsed['slots'].get(name)
        if value and not re.search(r'(?<![A-Za-z0-9_.-])' + re.escape(value) + r'(?![A-Za-z0-9_.-])', text):
            raise ActionError(f'{name} must appear verbatim in the user request')
    module = parsed['slots'].get('module')
    if module and module != _extract_module(text, root):
        raise ActionError('Module must match a name or alias in the request')
    return parsed


def llm_route(text: str, client, root, modules=None) -> Route:
    modules = modules if modules is not None else list_modules(root)
    system = _load_prompt(modules)
    result = client.chat_json(
        role="tui_router",
        system=system,
        user=text,
        schema_name="tui_route",
        validator=lambda parsed: _validate_input_route(parsed, text, root, modules),
    )
    parsed = result["parsed"]
    return Route(parsed.get("intent"), parsed.get("slots") or {},
                 str(parsed.get("reply") or ""), source="llm")


def route(text: str, root, client=None) -> Route:
    """Route NL text; the LLM path is attempted first and falls back to keywords."""
    local = keyword_route(text, root)
    if _readonly_request(text) or local.intent == 'clarify':
        return local
    if client is not None:
        try:
            return constrain_route(llm_route(text, client, root), text, root)
        except Exception:
            # Deterministic fallback retains the same local safety gates.
            pass
    return local

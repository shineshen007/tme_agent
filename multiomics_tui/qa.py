"""Read-only retrieval Q&A over frozen artifacts.

Retrieval is fully deterministic: excerpts are shown verbatim with file paths and
line numbers. When an LLM is available it may rewrite an answer, but the answer is
guarded (no statistics numbers, hedged language, citations must exist) and any
violation silently degrades to raw retrieval.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from .qa_guard import GUARD_VERSION, validate_tui_summary

PROMPT_PATH = Path(__file__).resolve().parent / "prompts/qa.md"

SOURCE_GLOBS = (
    "workspace/runs/*/report/*.md",
    "workspace/runs/*/report/agent/*.json",
    "CURRENT_STATE.md",
    "result/*/README.md",
    "result/README.md",
    "analysis/*/reports/*.md",
    "analysis/*/*/reports/*.md",
    "analysis/*/research/attempt_*/research_report.md",
    "docs/analysis_flowchart.md",
)

MAX_CHUNK_CHARS = 4000
MAX_EXCERPT_CHARS = 1600
JSON_STRING_MIN = 40

CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
# Gene/compound-like tokens such as "3MT" are valid terms; pure numbers are not.
LATIN_RE = re.compile(r"(?=[A-Za-z0-9_:.-]*[A-Za-z])[A-Za-z0-9][A-Za-z0-9_:.-]{2,}")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")

STOP_BIGRAMS = {"什么", "怎么", "哪些", "一下", "是否", "有没有", "多少", "可以", "能否", "结果",
                "分析", "数据", "帮我", "请问", "关于", "这个", "那个", "现在", "已经"}


@dataclass
class Chunk:
    source: str  # path relative to project root
    line: int | None
    heading: str
    text: str
    cid: str = ""  # assigned per query: S1, S2, ...
    score: float = 0.0

    def as_dict(self, limit: int = MAX_EXCERPT_CHARS) -> dict:
        text = self.text if len(self.text) <= limit else self.text[:limit] + " …"
        return {"id": self.cid, "source": self.source, "line": self.line,
                "heading": self.heading, "text": text, "score": round(self.score, 4)}


def _iter_files(root: Path):
    for pattern in SOURCE_GLOBS:
        for path in sorted(root.glob(pattern)):
            if path.is_file():
                yield path


def _chunks_markdown(path: Path, relative: str) -> list[Chunk]:
    chunks: list[Chunk] = []
    heading = path.name
    buffer: list[str] = []
    start_line = 1

    def flush() -> None:
        nonlocal buffer
        text = "\n".join(buffer).strip()
        if text:
            chunks.append(Chunk(relative, start_line, heading, text[:MAX_CHUNK_CHARS]))
        buffer = []

    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for number, line in enumerate(lines, start=1):
        match = HEADING_RE.match(line)
        if match:
            flush()
            heading = match.group(2).strip() or path.name
            start_line = number
            continue
        buffer.append(line)
    flush()
    if not chunks:
        text = "\n".join(lines).strip()
        if text:
            chunks.append(Chunk(relative, 1, path.name, text[:MAX_CHUNK_CHARS]))
    return chunks


def _walk_strings(node, path=()):
    if isinstance(node, str):
        yield ".".join(str(part) for part in path), node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield from _walk_strings(value, path + (key,))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk_strings(value, path + (index,))


def _chunks_json(path: Path, relative: str) -> list[Chunk]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    chunks = []
    for field_path, text in _walk_strings(payload):
        text = text.strip()
        if len(text) >= JSON_STRING_MIN:
            chunks.append(Chunk(relative, None, f"{relative}#{field_path}", text[:MAX_CHUNK_CHARS]))
    return chunks


def collect_chunks(root) -> list[Chunk]:
    root = Path(root)
    chunks: list[Chunk] = []
    for path in _iter_files(root):
        relative = path.relative_to(root).as_posix()
        if path.suffix.lower() == ".json":
            chunks.extend(_chunks_json(path, relative))
        else:
            chunks.extend(_chunks_markdown(path, relative))
    return chunks


def _tokens(question: str) -> list[str]:
    tokens: list[str] = []
    for match in LATIN_RE.findall(question or ""):
        tokens.append(match.lower())
    for run in CJK_RE.findall(question or ""):
        if len(run) == 1:
            tokens.append(run)
        else:
            for index in range(len(run) - 1):
                bigram = run[index:index + 2]
                if bigram not in STOP_BIGRAMS:
                    tokens.append(bigram)
    return tokens


def search(chunks: list[Chunk], question: str, k: int = 3) -> list[Chunk]:
    tokens = _tokens(question)
    for chunk in chunks:
        chunk.score = 0.0
    if not tokens or not chunks:
        return []
    lowered = [chunk.text.lower() for chunk in chunks]
    headings = [(chunk.heading or "").lower() for chunk in chunks]
    n = len(chunks)
    scored: list[Chunk] = []
    for token in set(tokens):
        df = sum(1 for text in lowered if token in text)
        if df == 0:
            continue
        idf = math.log(1 + n / (1 + df))
        for index, chunk in enumerate(chunks):
            tf = lowered[index].count(token)
            if not tf:
                continue
            score = (tf ** 0.5) * idf
            if token in headings[index]:
                score *= 2
            chunk.score = chunk.score + score
    for chunk in chunks:
        if chunk.score > 0:
            scored.append(chunk)
    scored.sort(key=lambda item: item.score, reverse=True)
    return scored[:k]


def _format_excerpts(chunks: list[Chunk]) -> str:
    parts = []
    for chunk in chunks:
        location = f"{chunk.source}:{chunk.line}" if chunk.line else chunk.source
        text = chunk.text if len(chunk.text) <= MAX_EXCERPT_CHARS else chunk.text[:MAX_EXCERPT_CHARS] + " …"
        parts.append(f"[{chunk.cid}] Source: {location}\nHeading: {chunk.heading}\n{text}")
    return "\n\n".join(parts)


def _cache_path(root: Path, key: str) -> Path:
    return root / ".cache/tui/qa" / f"{key}.json"


def _cache_key(question: str, chunks: list[Chunk], model: str) -> str:
    payload = json.dumps({"q": question, "chunks": [[c.source, c.line, c.text] for c in chunks],
                          "model": model, "guard_version": GUARD_VERSION}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def validate_summary(parsed: dict, chunks: list[Chunk], language: str = "en") -> dict:
    if not isinstance(parsed, dict):
        raise ValueError("Q&A output must be a JSON object")
    answer = str(parsed.get("answer") or "").strip()
    if not answer:
        raise ValueError("Q&A output has no answer")
    known = {chunk.cid for chunk in chunks}
    citations = parsed.get("citations") or []
    if not isinstance(citations, list):
        raise ValueError("Citations must be an array")
    unknown = sorted(set(str(item) for item in citations) - known)
    if unknown:
        raise ValueError(f"Unknown citation: {unknown}")
    mentioned = set(re.findall(r"\[(S\d+)\]", answer))
    if mentioned - known:
        raise ValueError(f"Answer cites unknown excerpts: {sorted(mentioned - known)}")
    validate_tui_summary(answer, citations, chunks, language=language)
    return {"answer": answer, "citations": sorted(set(str(item) for item in citations))}


def answer(root, question: str, client=None, k: int = 3, cache: bool = True, summary: bool = False, language: str = "en", knowledge_settings=None) -> dict:
    root = Path(root)
    chunks = collect_chunks(root)
    from orchestrator.knowledge_context import knowledge_service
    for entry in knowledge_service(root, knowledge_settings).search(question, limit=k):
        identifier = entry.get('entry_id', entry.get('id'))
        sources = '\n'.join(f"{source['source_id']}: {source['locator']}" for source in entry['sources'])
        chunks.append(Chunk(source=f"wiki:{entry.get('scope', 'project')}:{identifier}@{entry['revision']}",
                            line=None, heading=entry['title'],
                            text=f"[{entry['kind']}; reviewed knowledge, not independent validation]\n{entry['statement']}\n{sources}"))
    selected = search(chunks, question, k=k)
    for index, chunk in enumerate(selected, start=1):
        chunk.cid = f"S{index}"
    excerpts = [chunk.as_dict() for chunk in selected]
    result = {"mode": "retrieval", "question": question, "answer": None,
              "citations": [], "excerpts": excerpts, "notice": None}
    if not selected:
        result["notice"] = "No relevant excerpt was found in the saved artifacts."
        return result
    weak_match = selected[0].score < 0.5
    if not summary:
        result["notice"] = "Showing original excerpts and sources. Use /ask --summary <question> for an AI summary."
        return result
    if client is None:
        result["notice"] = ("AI summary is unavailable (missing API key or --no-llm); showing source excerpts."
                            if not weak_match else
                            "No close match was found; the excerpts below may be only loosely related.")
        return result

    model = getattr(client, "cfg", {}).get("model", "")
    key = _cache_key(question, selected, model + ":reply-" + language)
    cache_file = _cache_path(root, key)
    if cache and cache_file.is_file():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if not isinstance(cached, dict):
                raise ValueError("Summary cache must be an object")
            if cached.get("guard_version") != GUARD_VERSION:
                raise ValueError("Old summary cache version is invalid")
            validated = validate_summary(cached, selected, language=language)
            result.update({"mode": "llm", "answer": validated["answer"],
                           "citations": validated["citations"], "notice": "Cached AI summary; review against sources"})
            return result
        except (OSError, json.JSONDecodeError, ValueError):
            pass

    template = PROMPT_PATH.read_text(encoding="utf-8")
    system, _, _ = template.partition("## User question")
    system = system.replace("{reply_language}", "Chinese" if language == "zh" else "English")
    user = f"User question: {question}\n\n## Source excerpts\n\n" + _format_excerpts(selected)
    try:
        response = client.chat_json(
            role="tui_qa", system=system, user=user, schema_name="tui_qa",
            validator=lambda parsed: validate_summary(parsed, selected, language=language),
        )
        validated = validate_summary(response["parsed"], selected, language=language)
    except Exception as exc:  # noqa: BLE001 - degrade to retrieval, never fail the UI
        logging.getLogger(__name__).warning("TUI summary rejected or unavailable: %s", type(exc).__name__)
        result["rejection_reason"] = "summary_validation_or_transport_failure:" + type(exc).__name__
        result["notice"] = f"AI summary was rejected or unavailable ({type(exc).__name__}); showing source excerpts."
        return result

    if cache:
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps({**validated, "guard_version": GUARD_VERSION}, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass
    result.update({"mode": "llm", "answer": validated["answer"], "citations": validated["citations"],
                   "notice": "AI summary; review against sources. Use cited excerpts for numbers."})
    return result

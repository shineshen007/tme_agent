"""Reply language inference; identifiers and quoted code are not natural language."""
import re


def detect_language(text: str, fallback: str = "en") -> str:
    text = text.strip()
    if text.startswith("/") and not text.startswith("/ask "):
        return fallback
    text = re.sub(r"^/ask\s+(?:--summary\s+)?", "", text)
    text = re.sub(r"```[\s\S]*?```|`[^`]*`", " ", text)
    text = re.sub(r"(?:https?://|[./~]|[A-Za-z]:\\)\S+|\b\S+/\S+", " ", text)
    text = re.sub(r"\b[\w.-]+\.(?:csv|tsv|rds|txt|json|py|R|yaml|pdf|svg)\b", " ", text)
    if re.match(r"^(?:import |from .* import |def |class |SELECT |print\(|library\(|[\w.]+\s*(?:=|<-))", text.strip()):
        return fallback
    chinese = len(re.findall(r"[\u3400-\u9fff]", text))
    english = len(re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", text))
    if not chinese and not english:
        return fallback
    return "zh" if chinese >= english else "en"


def choose(language: str, english: str, chinese: str) -> str:
    return chinese if language == "zh" else english

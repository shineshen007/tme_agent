"""Reply language inference; identifiers and quoted code are not natural language."""
import re


def detect_language(text: str, fallback: str = "en") -> str:
    text = text.strip()
    if text.startswith("/") and not text.startswith("/ask "):
        return fallback
    text = re.sub(r"^/ask\s+(?:--summary\s+)?", "", text)
    text = re.sub(r'```[\s\S]*?```|`[^`]*`|"[^"\n]*"|“[^”\n]*”|「[^」\n]*」', " ", text)
    text = re.sub(r"(?<!\w)'[^'\n]+'(?!\w)", " ", text)
    text = re.sub(r"(?<!\w)(?:https?://|[./~]|[A-Za-z]:\\)\S+|\b\S+/\S+", " ", text)
    text = re.sub(r"\b[\w.-]+\.(?:csv|tsv|rds|gmt|txt|json|py|r|ya?ml|pdf|svg|xlsx?|xlsm|h5ad|h5|hdf5|parquet|feather|gz|zip)\b", " ", text, flags=re.I)
    if re.match(r"^(?:import |from .* import |def |class |SELECT |print\(|library\(|[\w.]+\s*(?:=|<-))", text.strip()):
        return fallback
    chinese = len(re.findall(r"[\u3400-\u9fff]", text))
    english = len(re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", text))
    if not chinese and not english:
        return fallback
    return "zh" if chinese else "en"


def choose(language: str, english: str, chinese: str) -> str:
    return chinese if language == "zh" else english

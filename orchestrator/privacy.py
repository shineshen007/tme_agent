"""Conservative outbound privacy checks shared by model, literature and Wiki paths.

These patterns are deliberately narrow: they target explicit patient/sample
identifiers and clinical free text, not ordinary biomedical prose. A match blocks
only the outbound operation; local computation and frozen results are untouched.
"""
from __future__ import annotations

import re


MISSING = {"", "na", "nan", "null", "none", "n/a", "unknown", "not_applicable"}

TCGA_BARCODE = re.compile(r"\bTCGA-[A-Za-z0-9]{2}-[A-Za-z0-9]{4}(?:-[A-Za-z0-9]+)*\b", re.I)
EXPLICIT_ID = re.compile(
    r"\b(?:patient|subject|donor|sample|case|specimen|biospecimen)[ _-]?"
    r"(?:id|no|number|identifier)[ \t]*[:=#][ \t]*[A-Za-z0-9][A-Za-z0-9_.:-]{1,}",
    re.I)
SEPARATED_ID = re.compile(
    r"(?<![/\\])\b(?:patient|subject|donor|sample|case)[_-][A-Za-z0-9]*\d[A-Za-z0-9_-]*\b"
    r"(?!\.(?:csv|tsv|txt|json|rds|xlsx?|pdf|gz|zip)\b)", re.I)
CLINICAL_ID = re.compile(
    r"\b(?:MRN|medical[ \t]+record(?:[ \t]+number)?|hospital[ \t]+number|"
    r"住院号|病历号|身份证(?:号)?)[ \t]*[:=#]?[ \t]*[A-Za-z0-9][A-Za-z0-9-]{2,}",
    re.I)
PERSON_NAME = re.compile(
    r"\b(?:patient|subject|donor)[ \t]+(?:name|initials)[ \t]*[:=][ \t]*[^\s,;]{2,}",
    re.I)
DOB = re.compile(r"\b(?:DOB|date[ \t]+of[ \t]+birth|出生日期)[ \t]*[:=][ \t]*[^\s,;]{4,}", re.I)
# A phone number must carry phone semantics: an explicit label, a leading "+", or
# a 3-3-4/3-4-4 grouping that stands alone. The lookarounds matter: a hex UUID such
# as 1e1587e8-8938-4578-8529-0831573a9fe1 contains "8938-4578-8529", whose digits sit
# between hex characters and hyphens, so it is not a phone number and must not block
# an outbound request. Dates, DOIs, versions and Ensembl IDs are likewise excluded.
CONTACT = re.compile(
    r"(?:(?:(?:phone|tel|telephone|mobile|cell|fax|contact|whatsapp)\b|"
    r"联系电话|电话|手机|联系方式)[ \t]*[:=：]?[ \t]*\(?\+?\d[\d \t().-]{5,}\d"
    r"|(?<![\w+-])\+\d{1,3}[ .-]?(?:\(\d{1,4}\)[ .-]?)?\d{2,4}[ .-]\d{3,4}[ .-]\d{3,4}(?![\w-])"
    r"|(?<![\w-])\(?\d{3,4}\)?[ .-]\d{3,4}[ .-]\d{4}(?![\w-])"
    r"|\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b)", re.I)
ADDRESS = re.compile(
    r"\b(?:address|住址|家庭住址|联系地址)[ \t]*[:=][ \t]*[^\n]{4,}", re.I)
LOCAL_PATH = re.compile(r"/(?:Users|home|private|tmp|Volumes)/|(?:^|\s)[A-Za-z]:[\\/]", re.I)
CREDENTIAL = re.compile(r"\bsk-[A-Za-z0-9_-]{24,}\b|-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----")

# Columns whose every nonmissing value is treated as an identifier.
IDENTIFIER_COLUMN = re.compile(
    r"(^|_)(patient|subject|donor|sample|case|specimen|biospecimen|mrn|medical_record|"
    r"record_number|accession|aliquot|barcode)(_?id|_?ids|_?no|_?number|_?key|_?code)?$",
    re.I)
FREE_TEXT_COLUMN = re.compile(
    r"(^|_)(patient_?name|name|notes?|comments?|history|address|phone|telephone|email|"
    r"dob|birth|mrn|medical_record|identifier|contact)$",
    re.I)

MAX_PRIVATE_VALUES = 20000


def redact_credentials(text, extra=()):
    """Replace caller-supplied credentials and known key patterns in free text.

    Used for transport errors, successful-response echoes and whole audit records.
    The actual authentication value is supplied by the caller (it may have no
    ``sk-`` prefix), while the regex catches obviously key-shaped values.
    """
    value = str(text)
    secrets = sorted({str(item) for item in extra if item and len(str(item)) >= 4},
                     key=len, reverse=True)
    for secret in secrets:
        value = value.replace(secret, '[REDACTED]')
    return CREDENTIAL.sub('[REDACTED]', value)


def _clean(value):
    return " ".join(str(value).split())


def _is_missing(value):
    return _clean(value).casefold() in MISSING


def is_identifier_column(name):
    return bool(IDENTIFIER_COLUMN.search(str(name or "").strip()))


def is_free_text_column(name):
    return bool(FREE_TEXT_COLUMN.search(str(name or "").strip()))


def sensitive_matches(text, private_ids=(), channel="model"):
    """Return (kind, matched text) pairs. Never rewrite or truncate the payload."""
    if not isinstance(text, str) or not text:
        return []
    matches = []
    folded = text.casefold()
    for value in private_ids or ():
        if not isinstance(value, str):
            continue
        cleaned = _clean(value)
        if len(cleaned) < 3 or cleaned.casefold() in MISSING:
            continue
        if cleaned.casefold() in folded:
            matches.append(("private_value", cleaned))
    patterns = [("tcga_barcode", TCGA_BARCODE), ("explicit_id", EXPLICIT_ID),
                ("separated_id", SEPARATED_ID), ("clinical_id", CLINICAL_ID),
                ("person_name", PERSON_NAME), ("dob", DOB), ("address", ADDRESS),
                ("local_path", LOCAL_PATH), ("credential", CREDENTIAL),
                ("contact", CONTACT)]
    for kind, pattern in patterns:
        for match in pattern.finditer(text):
            matches.append((kind, match.group(0)))
    # Preserve deterministic order and remove duplicates without hiding a category.
    seen, result = set(), []
    for kind, value in matches:
        key = (kind, value.casefold())
        if key in seen:
            continue
        seen.add(key)
        result.append((kind, value))
    return result


def metadata_private_values(row, keys=(), limit=MAX_PRIVATE_VALUES):
    """Collect explicit IDs and whole clinical free-text values from one metadata row.

    Free text is retained verbatim so an outbound model/literature/publish request
    containing the same note can be refused. The return value stays local.
    """
    values = set()
    for key in keys:
        value = row.get(key)
        if value is not None and not _is_missing(value):
            values.add(_clean(value))
    for name, value in row.items():
        if value is None or _is_missing(value):
            continue
        cleaned = _clean(value)
        if is_identifier_column(name) and len(cleaned) >= 3:
            values.add(cleaned)
        elif is_free_text_column(name) and len(cleaned) >= 4:
            values.add(cleaned[:500])
        if len(cleaned) <= 500:
            for pattern in (TCGA_BARCODE, EXPLICIT_ID, SEPARATED_ID, CLINICAL_ID, CONTACT):
                for match in pattern.finditer(cleaned):
                    found = match.group(0)
                    if found.casefold() not in MISSING and len(found) >= 3:
                        values.add(found)
    return sorted(values)[:limit]

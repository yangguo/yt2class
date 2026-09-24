"""Sanitize learner-visible titles and bullets (not internal notes/diagnostics)."""

from __future__ import annotations

import re

_META_INLINE_RE = re.compile(
    r"(ASR\s*[→\->]|OCR\s*[→\->]|误听|老师过渡|老师确认|板书对应|板书写了|板书同步|过渡语|"
    r"\btimed\b|ASR\s*only|字幕不足|metadata/|source_time=)",
    re.I,
)
_META_PAREN_RE = re.compile(
    r"[（(][^）)]*(?:ASR|误听|OCR|板书|过渡|metadata)[^）)]*[）)]",
    re.I,
)
_STANDALONE_ASR_NOTE_RE = re.compile(
    r"^(?:名刺|2月|二月|ニ月|２月)(?:[。．…]|$)",
)
_HEADWORD_TSUKI_RE = re.compile(r"(～|〜)?につき")
_TSUKI_MISHEAR_RE = re.compile(r"(?<![0-9０-９])2月(?![0-9０-９日号])")
_AUTOMATION_ID_RE = re.compile(
    r"(?:\bcap-\d+\b|\bocc-\d+\b|\basset-occ-\d+\b|"
    r"[A-Za-z0-9_.-]+\.webm(?:\s*@\s*\d{1,2}:\d{2})?|"
    r"\bwebm\s*@\s*\d{1,2}:\d{2})",
    re.I,
)
_ALIGNMENT_NOTE_RE = re.compile(
    r"(?:语音|画面|字幕).{0,12}(?:对应|对齐|同期)|\balignment\b",
    re.I,
)
_PROPORTION_TSUKI_RE = re.compile(
    r"(?:時間|一日|1日|１日|[0-9０-９]+日).{0,8}につき|"
    r"につき.{0,16}(?:[0-9０-９]+円|ポイント)|"
    r"[0-9０-９]+円.{0,20}につき"
)
LEARNER_ARTIFACT_RE = re.compile(r"cap-\d+|occ-\d+|webm\s*@", re.I)


def contains_automation_artifact(text: str) -> bool:
    if not text:
        return False
    return LEARNER_ARTIFACT_RE.search(text) is not None


def contains_student_meta(text: str) -> bool:
    if not text or not text.strip():
        return False
    if _META_INLINE_RE.search(text):
        return True
    if _META_PAREN_RE.search(text):
        return True
    if _AUTOMATION_ID_RE.search(text) or _ALIGNMENT_NOTE_RE.search(text):
        return True
    stripped = text.strip()
    if _STANDALONE_ASR_NOTE_RE.match(stripped):
        return True
    return False


def normalize_headword_display(text: str) -> str:
    if not text:
        return text
    if re.match(r"用法[一二三四五六七八九十]+：", text.strip()):
        return text
    if _PROPORTION_TSUKI_RE.search(text):
        return text
    if _HEADWORD_TSUKI_RE.search(text) and not re.search(r"時間につき", text):
        return _HEADWORD_TSUKI_RE.sub("～につき", text, count=1)
    if "用法" in text or "文法" in text or "助詞" in text:
        text = _TSUKI_MISHEAR_RE.sub("～につき", text)
    return text


def sanitize_student_copy(text: str) -> str:
    """Return text safe for slide titles and learner bullets."""

    if not text:
        return text
    cleaned = _META_PAREN_RE.sub("", text)
    cleaned = _META_INLINE_RE.sub("", cleaned)
    cleaned = _AUTOMATION_ID_RE.sub("", cleaned)
    cleaned = _ALIGNMENT_NOTE_RE.sub("", cleaned)
    parts = re.split(r"([。．！？\n])", cleaned)
    kept: list[str] = []
    for index in range(0, len(parts), 2):
        chunk = parts[index].strip()
        delimiter = parts[index + 1] if index + 1 < len(parts) else ""
        if not chunk:
            continue
        if contains_student_meta(chunk):
            continue
        kept.append(chunk + delimiter)
    result = "".join(kept).strip()
    if not result:
        if contains_student_meta(text):
            return ""
        result = cleaned.strip()
    result = re.sub(r"\s{2,}", " ", result)
    result = normalize_headword_display(result)
    return result.strip()


__all__ = [
    "LEARNER_ARTIFACT_RE",
    "contains_automation_artifact",
    "contains_student_meta",
    "normalize_headword_display",
    "sanitize_student_copy",
]

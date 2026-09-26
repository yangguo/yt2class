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
    r"(?:语音|画面|字幕|板书).{0,16}(?:对应|对齐|同期|相对应)|对齐说明|\balignment\b",
    re.I,
)
_AUTHORITY_NOTE_RE = re.compile(r"应?以[^。．！？\n；;]{0,48}为准")
_ASR_DIGIT_GLOSS_RE = re.compile(r"(?<![0-9０-９A-Za-z])[0-9０-９]{1,2}効果")
_CORRECTION_CHATTER_RE = re.compile(
    r"(?:OCR|ASR|字幕|语音|识别).{0,16}(?:校正|纠正|修正|误识|错听)|"
    r"(?:校正|纠正|误识)(?:说明|注记|备注)|"
    r"(?:校正|纠正|误识)\s*[:：]"
)
_QUOTED_SPAN = r"(?:「[^」]{0,80}」|『[^』]{0,80}』|“[^”]{0,80}”|\"[^\"]{0,80}\")"
# "ASR 将「通行不可」为「」" and close variants. The target quote may be empty.
_ASR_MISHEAR_RE = re.compile(
    rf"(?:ASR|语音识别)\s*(?:将|把)\s*"
    rf"(?:{_QUOTED_SPAN}|[^。．！？\n；;]{{1,40}}?)"
    rf"\s*(?:识别为|听成|听作|误识为|误听为|为)\s*"
    rf"(?:{_QUOTED_SPAN}|[^。．！？\n；;，,]{{0,24}})?"
    rf"\s*[，,、]*",
    re.I,
)
_TIMING_RANGE_RE = re.compile(
    r"(?<![A-Za-z0-9.])\d+(?:\.\d+)?\s*[–—\-－~〜]\s*\d+(?:\.\d+)?\s*s\b",
    re.I,
)
_TIMING_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9.])\d+(?:\.\d+)?s\b", re.I)
# Media offsets such as 「约374.46秒起」, not a taught duration like 「约30秒」.
_APPROX_OFFSET_RE = re.compile(
    r"[，,、]?\s*(?:大约|大約|约|約)\s*(?:\d{3,}(?:\.\d+)?|\d+\.\d+)\s*秒(?:起)?"
)
_EMPTY_BOARD_FRAME_RE = re.compile(r"板书帧\s*[（(]\s*[）)]")
_BOARD_LABEL_RE = re.compile(r"板书对应\s*[:：]?")
_PROPORTION_TSUKI_RE = re.compile(
    r"(?:時間|一日|1日|１日|[0-9０-９]+日).{0,8}につき|"
    r"につき.{0,16}(?:[0-9０-９]+円|ポイント)|"
    r"[0-9０-９]+円.{0,20}につき"
)
_ANALYSIS_MARKER_RE = re.compile(
    r"新知识导入(?:[（(][^）)]*[）)])?|保留条件|保留否定|对比点|老师指出|老师宣布"
)
_CONDITION_EDIT_LABEL_RE = re.compile(r"保留条件\s*[:：]?")
_ANALYSIS_PREFIX_RE = re.compile(
    r"^\s*(?:新知识导入(?:[（(][^）)]*[）)])?|保留条件|保留否定|对比点)\s*[:：]?\s*"
)
_ANALYSIS_NARRATION_RE = re.compile(r"(?:老师指出|老师宣布|老师强调|对比点|保留否定)")
LEARNER_ARTIFACT_RE = re.compile(r"cap-\d+|occ-\d+|webm\s*@", re.I)


def contains_automation_artifact(text: str) -> bool:
    if not text:
        return False
    return LEARNER_ARTIFACT_RE.search(text) is not None


def _is_timing_alignment(text: str) -> bool:
    """Board-sync notes that cite a timestamp or an empty 板书帧（）."""

    if _EMPTY_BOARD_FRAME_RE.search(text):
        return True
    has_timing = _TIMING_RANGE_RE.search(text) is not None or _TIMING_TOKEN_RE.search(text) is not None
    if not has_timing:
        return False
    return any(token in text for token in ("板书", "相对应", "开场", "帧"))


def _strip_diagnostic_residue(text: str) -> str:
    """Drop ASR/OCR correction notes while leaving the example and gloss."""

    cleaned = _ASR_DIGIT_GLOSS_RE.sub("", text)
    cleaned = _AUTHORITY_NOTE_RE.sub("", cleaned)
    cleaned = _CORRECTION_CHATTER_RE.sub("", cleaned)
    cleaned = _ASR_MISHEAR_RE.sub("", cleaned)
    # The mishear clause is usually glued on with ； and closed by a dangling 。
    cleaned = re.sub(r"[；;]\s*[。．]", "", cleaned)
    cleaned = re.sub(r"[；;]\s*$", "", cleaned)
    cleaned = re.sub(r"[，,、]+\s*$", "", cleaned)
    cleaned = re.sub(r"[：:]\s*(?=[。．！？])", "", cleaned)
    cleaned = re.sub(r"(?<=[。．！？])\s*[，,、：:]\s*", "", cleaned)
    cleaned = re.sub(r"。{2,}", "。", cleaned)
    cleaned = re.sub(r"^[，,、：:\s]+", "", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned


def _strip_timing_scaffolds(text: str) -> str:
    cleaned = _APPROX_OFFSET_RE.sub("", text)
    cleaned = _TIMING_RANGE_RE.sub("", cleaned)
    cleaned = _TIMING_TOKEN_RE.sub("", cleaned)
    cleaned = _EMPTY_BOARD_FRAME_RE.sub("", cleaned)
    cleaned = _BOARD_LABEL_RE.sub("", cleaned)
    cleaned = re.sub(r"^[：:、，\s]+", "", cleaned)
    return cleaned


def _strip_analysis_fragments(text: str) -> str:
    cleaned = _ANALYSIS_PREFIX_RE.sub("", text)
    chunks = re.split(r"([；;。．！？\n])", cleaned)
    kept: list[str] = []
    for index in range(0, len(chunks), 2):
        chunk = chunks[index].strip()
        delimiter = chunks[index + 1] if index + 1 < len(chunks) else ""
        if not chunk:
            continue
        if "保留否定" in chunk or "新知识导入" in chunk or "复习内容" in chunk:
            continue
        chunk = _CONDITION_EDIT_LABEL_RE.sub("", chunk)
        chunk = re.sub(r"老师(?:指出|宣布|强调)\s*", "", chunk)
        chunk = re.sub(r"^[—–-]+\s*", "", chunk).strip()
        if chunk:
            kept.append(chunk + delimiter)
    return "".join(kept)


def contains_student_meta(text: str) -> bool:
    if not text or not text.strip():
        return False
    if _META_INLINE_RE.search(text):
        return True
    if _META_PAREN_RE.search(text):
        return True
    if _AUTOMATION_ID_RE.search(text) or _ALIGNMENT_NOTE_RE.search(text):
        return True
    if _ANALYSIS_MARKER_RE.search(text):
        return True
    if _is_timing_alignment(text):
        return True
    if _ASR_DIGIT_GLOSS_RE.search(text) or _AUTHORITY_NOTE_RE.search(text):
        return True
    if _CORRECTION_CHATTER_RE.search(text) or _ASR_MISHEAR_RE.search(text):
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
    match = _HEADWORD_TSUKI_RE.search(text)
    if match and not re.search(r"時間につき", text):
        prefix = text[: match.start()]
        # A preceding Japanese character means this occurrence is sentence text.
        standalone = not prefix or prefix[-1].isspace() or prefix[-1] in "：:、，,（([「『\"'"
        labeled = bool(re.search(r"(?:文法|语法|詞頭|词头|词条)\s*[：:、，,]?\s*$", prefix))
        if standalone or labeled:
            return text[: match.start()] + "～につき" + text[match.end() :]
    if "用法" in text or "文法" in text or "助詞" in text:
        text = _TSUKI_MISHEAR_RE.sub("～につき", text)
    return text


def sanitize_student_copy(text: str) -> str:
    """Return text safe for slide titles and learner bullets."""

    if not text:
        return text
    cleaned = _strip_analysis_fragments(text)
    cleaned = _META_PAREN_RE.sub("", cleaned)
    cleaned = _META_INLINE_RE.sub("", cleaned)
    cleaned = _AUTOMATION_ID_RE.sub("", cleaned)
    cleaned = _ALIGNMENT_NOTE_RE.sub("", cleaned)
    cleaned = _strip_diagnostic_residue(cleaned)
    parts = re.split(r"([。．！？\n])", cleaned)
    kept: list[str] = []
    for index in range(0, len(parts), 2):
        chunk = parts[index].strip()
        delimiter = parts[index + 1] if index + 1 < len(parts) else ""
        if not chunk:
            continue
        if _is_timing_alignment(chunk) or contains_student_meta(chunk):
            continue
        chunk = _strip_timing_scaffolds(chunk).strip()
        if not chunk:
            continue
        kept.append(chunk + delimiter)
    result = "".join(kept).strip()
    if not result:
        if contains_student_meta(text) or _is_timing_alignment(text):
            return ""
        result = _strip_timing_scaffolds(cleaned).strip()
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

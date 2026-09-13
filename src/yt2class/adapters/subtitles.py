"""Caption track selection and normalization into :class:`TranscriptDocument`.

The adapter deliberately keeps the source track separate from the normalized
segments.  A sidecar or caption file is evidence; cleaning markup or removing
scroll duplicates must not make it look like ASR output.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from html import unescape
from pathlib import Path
import re
from typing import Iterable, Sequence

from yt2class.domain.transcript import (
    SpeechCoverage,
    TranscriptDocument,
    TranscriptGap,
    TranscriptOrigin,
    TranscriptSegment,
)


class SubtitleError(ValueError):
    """Raised when a subtitle artifact cannot be read or normalized."""


@dataclass(frozen=True)
class SubtitleCue:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class SubtitleTrack:
    path: Path
    language: str = "und"
    origin: TranscriptOrigin = "sidecar"


@dataclass(frozen=True)
class SubtitleParseResult:
    cues: tuple[SubtitleCue, ...]
    issues: tuple[tuple[str, float | None, float | None], ...] = ()


_TAG = re.compile(r"<[^>]*>")
_ASS_TAG = re.compile(r"\{\\[^}]*\}")
_TIMESTAMP = re.compile(
    r"^(?:(?P<hours>\d{1,2}):)?(?P<minutes>\d{2}):(?P<seconds>\d{2})(?P<fraction>[.,]\d{1,3})$"
)


def _parse_timestamp(value: str) -> float:
    match = _TIMESTAMP.fullmatch(value.strip())
    if not match:
        # WebVTT also permits MM:SS.mmm without an hours component.
        short = re.fullmatch(r"(?P<minutes>\d{1,3}):(?P<seconds>\d{2})(?P<fraction>[.,]\d{1,3})", value.strip())
        if not short:
            raise SubtitleError(f"invalid subtitle timestamp: {value!r}")
        hours = 0
        minutes = int(short.group("minutes"))
        seconds = int(short.group("seconds"))
        fraction = short.group("fraction")[1:].ljust(3, "0")
    else:
        hours = int(match.group("hours") or 0)
        minutes = int(match.group("minutes"))
        seconds = int(match.group("seconds"))
        fraction = match.group("fraction")[1:].ljust(3, "0")
    if minutes >= 60 or seconds >= 60:
        raise SubtitleError(f"invalid subtitle timestamp: {value!r}")
    return hours * 3600.0 + minutes * 60.0 + seconds + int(fraction) / 1000.0


def _clean_text(lines: Iterable[str]) -> str:
    cleaned: list[str] = []
    for line in lines:
        value = unescape(_ASS_TAG.sub("", _TAG.sub("", line))).strip()
        if value:
            cleaned.append(value)
    return " ".join(cleaned)


def _parse_document(content: str, *, webvtt: bool) -> SubtitleParseResult:
    lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    cues: list[SubtitleCue] = []
    issues: list[tuple[str, float | None, float | None]] = []
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if not line or line.upper() == "WEBVTT" or line.startswith("NOTE"):
            index += 1
            continue
        if "-->" not in line:
            index += 1
            continue
        left, right = line.split("-->", 1)
        # Cue settings follow the end timestamp in VTT.
        start_text = left.strip().split(maxsplit=1)[0]
        end_text = right.strip().split(maxsplit=1)[0]
        try:
            start = _parse_timestamp(start_text)
            end = _parse_timestamp(end_text)
        except SubtitleError:
            issues.append(("invalid-time", None, None))
            index += 1
            while index < len(lines) and lines[index].strip():
                index += 1
            continue
        index += 1
        text_lines: list[str] = []
        while index < len(lines) and lines[index].strip():
            text_lines.append(lines[index])
            index += 1
        text = _clean_text(text_lines)
        if not start < end:
            issues.append(("invalid-range", start, end if end > start else None))
        elif not text:
            issues.append(("empty-caption", start, end))
        else:
            cues.append(SubtitleCue(start=start, end=end, text=text))
    return SubtitleParseResult(tuple(cues), tuple(issues))


def parse_vtt(content: str) -> list[SubtitleCue]:
    """Parse valid WebVTT cues, ignoring malformed cues for compatibility."""

    return list(_parse_document(content, webvtt=True).cues)


def parse_srt(content: str) -> list[SubtitleCue]:
    """Parse valid SRT cues, using the same normalized cue representation."""

    return list(_parse_document(content, webvtt=False).cues)


def _parse_path(path: Path) -> SubtitleParseResult:
    try:
        content = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as error:
        raise SubtitleError(f"cannot read subtitle artifact: {path}") from error
    suffix = path.suffix.lower()
    if suffix == ".srt":
        return _parse_document(content, webvtt=False)
    if suffix in {".vtt", ".webvtt", ""}:
        return _parse_document(content, webvtt=True)
    raise SubtitleError(f"unsupported subtitle format: {path.suffix}")


def _as_tracks(value: object, origin: TranscriptOrigin) -> list[SubtitleTrack]:
    if value is None:
        return []
    values: Sequence[object]
    if isinstance(value, (str, Path, SubtitleTrack)):
        values = [value]
    else:
        values = list(value)  # type: ignore[arg-type]
    tracks: list[SubtitleTrack] = []
    for item in values:
        track = item if isinstance(item, SubtitleTrack) else SubtitleTrack(Path(item), origin=origin)
        path = track.path.expanduser()
        try:
            usable = path.is_file() and path.stat().st_size > 0
        except OSError:
            usable = False
        if usable:
            tracks.append(SubtitleTrack(path=path, language=track.language, origin=track.origin))
    return tracks


def _infer_language(path: Path, fallback: str) -> str:
    if fallback and fallback != "und":
        return fallback
    for token in reversed(path.stem.split(".")):
        if re.fullmatch(r"[A-Za-z]{2,3}(?:[-_][A-Za-z]{2,4})?", token):
            return token.replace("_", "-")
    tokens = re.split(r"[._-]", path.stem)
    for token in reversed(tokens):
        if len(token) in {2, 3} and token.isalpha():
            return token
    return "und"


def choose_subtitle_track(
    *,
    sidecar: object = None,
    manual: object = None,
    auto: object = None,
    language: str | None = None,
) -> SubtitleTrack | None:
    """Choose one usable track in the explicit sidecar/manual/auto order."""

    groups = (
        ("sidecar", _as_tracks(sidecar, "sidecar")),
        ("manual-caption", _as_tracks(manual, "manual-caption")),
        ("auto-caption", _as_tracks(auto, "auto-caption")),
    )
    for _origin, tracks in groups:
        if not tracks:
            continue
        normalized = [
            SubtitleTrack(
                path=track.path,
                language=_infer_language(track.path, track.language),
                origin=track.origin,
            )
            for track in tracks
        ]
        for track in normalized:
            if language is None or track.language in {"und", language}:
                return SubtitleTrack(
                    path=track.path,
                    language=language or track.language,
                    origin=track.origin,
                )
        if language is not None:
            continue
        track = normalized[0]
        return SubtitleTrack(
            path=track.path,
            language=track.language,
            origin=track.origin,
        )
    return None


def _deduplicate_scroll(cues: Iterable[SubtitleCue]) -> list[SubtitleCue]:
    result: list[SubtitleCue] = []
    for cue in sorted(cues, key=lambda item: (item.start, item.end, item.text)):
        if result and cue.start < result[-1].end:
            previous = result[-1]
            previous_words = re.sub(r"\s+", " ", previous.text.strip()).casefold().split()
            current_words = re.sub(r"\s+", " ", cue.text.strip()).casefold().split()
            if current_words == previous_words:
                continue
            if (
                len(current_words) > len(previous_words)
                and current_words[: len(previous_words)] == previous_words
            ):
                # Rolling captions often repeat the previous words while
                # appending newly revealed words.  Keep the latest complete
                # text and the union of both cue intervals so no suffix or
                # source time is discarded.
                result[-1] = SubtitleCue(
                    start=previous.start,
                    end=max(previous.end, cue.end),
                    text=cue.text,
                )
                continue
        result.append(cue)
    return result


def _covered_ranges(cues: Iterable[SubtitleCue], duration: float) -> list[tuple[float, float]]:
    ranges = sorted((max(0.0, cue.start), min(duration, cue.end)) for cue in cues)
    ranges = [item for item in ranges if item[0] < item[1]]
    merged: list[list[float]] = []
    for start, end in ranges:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def _make_gaps(
    covered: list[tuple[float, float]],
    duration: float,
    issues: Iterable[tuple[str, float | None, float | None]],
) -> list[TranscriptGap]:
    gaps: list[TranscriptGap] = []
    cursor = 0.0
    for start, end in covered:
        if cursor < start:
            gaps.append(TranscriptGap(id=f"gap-{len(gaps)+1:04d}", start_seconds=cursor, end_seconds=start, reason="uncovered"))
        cursor = max(cursor, end)
    if cursor < duration:
        gaps.append(TranscriptGap(id=f"gap-{len(gaps)+1:04d}", start_seconds=cursor, end_seconds=duration, reason="uncovered"))
    for reason, start, end in issues:
        if start is not None and end is not None and start < end:
            clipped_start = max(0.0, min(duration, start))
            clipped_end = max(0.0, min(duration, end))
            if clipped_start < clipped_end:
                gaps.append(
                    TranscriptGap(
                        id=f"gap-{len(gaps)+1:04d}",
                        start_seconds=clipped_start,
                        end_seconds=clipped_end,
                        reason=reason,
                    )
                )
        else:
            gaps.append(
                TranscriptGap(
                    id=f"gap-{len(gaps)+1:04d}",
                    start_seconds=0.0,
                    end_seconds=duration,
                    reason=reason,
                )
            )
    return gaps


def build_transcript_document(
    track: SubtitleTrack | str | Path,
    *,
    source_id: str,
    duration_seconds: float,
    language: str = "und",
    origin: TranscriptOrigin = "sidecar",
) -> TranscriptDocument:
    """Normalize one selected subtitle track with deterministic IDs and coverage."""

    if duration_seconds <= 0:
        raise SubtitleError("subtitle source duration must be positive")
    if not isinstance(track, SubtitleTrack):
        track = SubtitleTrack(path=Path(track), language=language, origin=origin)
    try:
        raw = track.path.read_bytes()
    except OSError as error:
        raise SubtitleError(f"cannot read subtitle artifact: {track.path}") from error
    parsed = _parse_path(track.path)
    raw_hash = sha256(raw).hexdigest()
    clipped: list[SubtitleCue] = []
    issues = list(parsed.issues)
    if not parsed.cues and not issues:
        issues.append(("empty-subtitle", None, None))
    for cue in _deduplicate_scroll(parsed.cues):
        start = max(0.0, cue.start)
        end = min(duration_seconds, cue.end)
        if start >= duration_seconds or end <= 0 or not start < end:
            issues.append(("outside-duration", cue.start, cue.end))
            continue
        clipped.append(SubtitleCue(start, end, cue.text))
    overlap_ids: set[int] = set()
    for index, cue in enumerate(clipped):
        if any(
            other_index != index
            and max(cue.start, other.start) < min(cue.end, other.end)
            for other_index, other in enumerate(clipped)
        ):
            overlap_ids.add(index)
    segments: list[TranscriptSegment] = []
    for index, cue in enumerate(clipped, start=1):
        flags = ["overlap"] if index - 1 in overlap_ids else []
        segments.append(
            TranscriptSegment(
                id=f"cap-{index:04d}",
                start_seconds=cue.start,
                end_seconds=cue.end,
                text_original=cue.text,
                language=track.language if len(track.language) >= 2 else "und",
                origin=track.origin,
                raw_ref=f"raw-{raw_hash[:16]}",
                alignment_status="unaligned",
                quality_flags=flags,
            )
        )
    covered = _covered_ranges(clipped, duration_seconds)
    covered_seconds = sum(end - start for start, end in covered)
    ratio = covered_seconds / duration_seconds if duration_seconds else 0.0
    gaps = _make_gaps(covered, duration_seconds, issues)
    return TranscriptDocument(
        schema_version="1.0",
        source_id=source_id,
        language=track.language if len(track.language) >= 2 else "und",
        raw_artifact_hash=raw_hash,
        alignment="sentence" if segments else "none",
        speech_coverage=SpeechCoverage(
            speech_seconds=covered_seconds,
            covered_seconds=covered_seconds,
            denominator="timeline",
            denominator_seconds=duration_seconds,
            coverage_ratio=ratio,
        ),
        segments=segments,
        duration_seconds=duration_seconds,
        status="complete" if segments and not issues and not gaps else "degraded",
        gaps=gaps,
    )


parse_subtitle_file = _parse_path
normalize_subtitle_track = build_transcript_document
select_subtitle_track = choose_subtitle_track
normalize_subtitles = build_transcript_document


__all__ = [
    "SubtitleCue",
    "SubtitleError",
    "SubtitleParseResult",
    "SubtitleTrack",
    "build_transcript_document",
    "choose_subtitle_track",
    "normalize_subtitle_track",
    "normalize_subtitles",
    "parse_srt",
    "parse_subtitle_file",
    "parse_vtt",
    "select_subtitle_track",
]

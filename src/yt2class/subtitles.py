"""Small, dependency-light WebVTT parser for frame-local context."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


@dataclass(frozen=True)
class Caption:
    start: float
    end: float
    text: str


_TIMESTAMP = re.compile(
    r"^(?:(?P<hours>\d{2}):)?(?P<minutes>\d{2}):(?P<seconds>\d{2})[.,](?P<millis>\d{3})$"
)
_TAG = re.compile(r"<[^>]+>")


def _parse_timestamp(value: str) -> float:
    match = _TIMESTAMP.match(value.strip())
    if not match:
        raise ValueError(f"Invalid WebVTT timestamp: {value}")
    hours = int(match.group("hours") or 0)
    return (
        hours * 3600
        + int(match.group("minutes")) * 60
        + int(match.group("seconds"))
        + int(match.group("millis")) / 1000
    )


def parse_vtt(content: str) -> list[Caption]:
    """Parse cue timing and visible text from a WebVTT document."""

    lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    cues: list[Caption] = []
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if "-->" not in line:
            index += 1
            continue
        start_value, end_value = [part.strip().split(" ", 1)[0] for part in line.split("-->", 1)]
        start = _parse_timestamp(start_value)
        end = _parse_timestamp(end_value)
        index += 1
        text_lines: list[str] = []
        while index < len(lines) and lines[index].strip():
            cleaned = _TAG.sub("", lines[index]).strip()
            if cleaned:
                text_lines.append(cleaned)
            index += 1
        text = " ".join(text_lines)
        if text and end >= start:
            cues.append(Caption(start, end, text))
    return cues


def nearby_text(cues: list[Caption], timestamp: float, window: float = 6.0) -> str:
    """Return unique cue text near a frame timestamp, preserving cue order."""

    selected: list[str] = []
    for cue in cues:
        if cue.end >= timestamp - window and cue.start <= timestamp + window:
            if cue.text not in selected:
                selected.append(cue.text)
    return " ".join(selected)


def load_vtt(path: Path) -> list[Caption]:
    return parse_vtt(path.read_text(encoding="utf-8-sig"))


def find_subtitle_file(
    media_dir: Path,
    languages: tuple[str, ...] = ("ja", "zh-Hans", "zh-Hant"),
) -> Path | None:
    """Choose the first downloaded subtitle language in the requested order."""

    for language in languages:
        candidate = media_dir / f"source.{language}.vtt"
        if candidate.exists() and candidate.stat().st_size:
            return candidate
    return None

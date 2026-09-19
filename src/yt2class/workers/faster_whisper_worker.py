"""Optional faster-whisper worker (lazy import; no model download at import time)."""

from __future__ import annotations

from pathlib import Path
from threading import Event
from typing import Any


class FasterWhisperUnavailable(RuntimeError):
    """Raised when the optional faster-whisper package is not installed."""


def transcribe_to_payload(
    audio_path: Path,
    *,
    model: str = "medium",
    language: str | None = None,
    device: str = "cpu",
    compute_type: str = "int8",
    cancel_event: Event | None = None,
) -> dict[str, Any]:
    """Transcribe audio to the shared ASR JSON envelope.

    When ``language`` is ``None``, faster-whisper autodetects per utterance. That
    is the recommended setting for mixed Japanese/Chinese lesson audio instead of
    forcing ``ja`` or ``zh``.
    """

    try:
        from faster_whisper import WhisperModel
    except ImportError as error:
        raise FasterWhisperUnavailable(
            "faster-whisper is not installed; pip install faster-whisper or "
            "pip install 'yt2class[asr]'"
        ) from error

    if cancel_event is not None and cancel_event.is_set():
        raise RuntimeError("ASR cancelled before faster-whisper start")

    whisper = WhisperModel(model, device=device, compute_type=compute_type)
    segments_iter, info = whisper.transcribe(
        str(audio_path),
        language=language,
        vad_filter=True,
    )
    segments: list[dict[str, Any]] = []
    for segment in segments_iter:
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("ASR cancelled during faster-whisper")
        text = (segment.text or "").strip()
        if not text:
            continue
        segments.append(
            {
                "start": float(segment.start),
                "end": float(segment.end),
                "text": text,
            }
        )
    detected = getattr(info, "language", None) or language or "und"
    return {
        "schema_version": "1.0",
        "engine": "faster-whisper",
        "model": model,
        "language": detected,
        "device": device,
        "alignment": "sentence",
        "diarization": False,
        "status": "complete",
        "segments": segments,
    }


__all__ = ["FasterWhisperUnavailable", "transcribe_to_payload"]

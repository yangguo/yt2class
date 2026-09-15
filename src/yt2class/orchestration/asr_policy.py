"""Resolve course ASR settings into extract_evidence requests."""

from __future__ import annotations

from pathlib import Path

from yt2class.adapters.asr import ASRRequest, resolve_asr_engine
from yt2class.config import AnalysisConfig
from yt2class.domain.source import SourceManifest


def build_asr_request(
    source: SourceManifest,
    *,
    workspace_root: Path,
    analysis: AnalysisConfig,
) -> tuple[ASRRequest | None, str | None]:
    engine, unavailable_reason = resolve_asr_engine(analysis.asr_engine)
    if engine == "none":
        return None, unavailable_reason
    return (
        ASRRequest(
            request_id=f"asr:{source.source_id}",
            source_id=source.source_id,
            audio_path=workspace_root / "tmp" / "asr-audio.wav",
            engine=engine,
            model=analysis.asr_model,
            language=analysis.asr_language,
            device=analysis.asr_device,
            align=engine == "whisperx",
            diarize=False,
        ),
        None,
    )


__all__ = ["build_asr_request"]

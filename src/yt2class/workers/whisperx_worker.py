"""Optional WhisperX worker.

WhisperX is deliberately imported only when this module is executed as a
worker.  The normal package and CI therefore do not download models or need
the heavy optional dependency.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


class WhisperXUnavailable(RuntimeError):
    """Raised when the optional WhisperX environment is not installed."""


def build_whisperx_command(
    audio_path: Path,
    *,
    model: str = "small",
    device: str = "cpu",
    language: str | None = None,
    align: bool = True,
    diarize: bool = False,
    offset_seconds: float = 0.0,
) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "yt2class.workers.whisperx_worker",
        "--input",
        str(Path(audio_path)),
        "--model",
        model,
        "--device",
        device,
        "--offset-seconds",
        f"{float(offset_seconds):.6f}",
    ]
    if language:
        command.extend(["--language", language])
    if align:
        command.append("--align")
    if diarize:
        command.append("--diarize")
    return command


def run_whisperx(
    audio_path: Path,
    *,
    model: str,
    device: str,
    language: str | None = None,
    align: bool = True,
    diarize: bool = False,
) -> dict[str, Any]:
    """Execute WhisperX only inside the explicitly selected worker process."""

    try:
        import whisperx  # type: ignore[import-not-found]
    except ImportError as error:
        raise WhisperXUnavailable(
            "WhisperX is optional; install the worker environment to run ASR"
        ) from error

    loaded = whisperx.load_model(model, device=device, compute_type="int8")
    result = loaded.transcribe(str(audio_path), language=language)
    if align and result.get("segments"):
        language_code = result.get("language") or language
        if language_code:
            metadata = whisperx.load_align_model(language_code=language_code, device=device)
            result = whisperx.align(
                result["segments"],
                metadata[0],
                metadata[1],
                str(audio_path),
                device,
                return_char_alignments=False,
            )
    if diarize and result.get("segments"):
        diarization = whisperx.DiarizationPipeline(use_auth_token=None, device=device)
        diarization_segments = diarization(str(audio_path))
        result["segments"] = whisperx.assign_word_speakers(diarization_segments, result["segments"])
        result["diarization"] = True
    result.setdefault("schema_version", "1.0")
    result.setdefault("engine", "whisperx")
    result.setdefault("model", model)
    result.setdefault("device", device)
    result.setdefault("alignment", "word" if align else "sentence")
    result.setdefault("diarization", diarize)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Optional yt2class WhisperX worker")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--model", default="small")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--language")
    parser.add_argument("--offset-seconds", type=float, default=0.0)
    parser.add_argument("--align", action="store_true")
    parser.add_argument("--diarize", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run_whisperx(
            args.input,
            model=args.model,
            device=args.device,
            language=args.language,
            align=args.align,
            diarize=args.diarize,
        )
    except WhisperXUnavailable as error:
        print(str(error), file=sys.stderr)
        return 2
    except Exception as error:  # pragma: no cover - optional runtime boundary
        print(f"WhisperX worker failed: {error}", file=sys.stderr)
        return 1
    result["offset_seconds"] = args.offset_seconds
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by subprocess
    raise SystemExit(main())


__all__ = [
    "WhisperXUnavailable",
    "build_whisperx_command",
    "main",
    "run_whisperx",
]

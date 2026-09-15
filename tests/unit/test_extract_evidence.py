from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from yt2class.adapters.asr import (
    ASRError,
    ASRRequest,
    ASRResult,
    ASRSegment,
    extract_audio_with_provenance,
)
from yt2class.domain.source import SourceManifest, content_sha256
from yt2class.domain.transcript import TranscriptWord
from yt2class.domain.visual import VisualCatalogue
from yt2class.stages.extract_evidence import _transcript_from_asr, extract_evidence


def _write_media(path: Path, payload: bytes = b"reference-media") -> Path:
    path.write_bytes(payload)
    return path


def _manifest(path: Path, *, source_id: str = "src-test") -> SourceManifest:
    return SourceManifest(
        schema_version="1.0",
        source_id=source_id,
        kind="local",
        title="test",
        media_path="media/source.mp4",
        sha256=content_sha256(path),
        duration_seconds=2.0,
        streams=[{"index": 0, "codec_type": "video", "codec_name": "h264"}],
        timebase="1/90000",
        local_mode="reference",
        reference_path=str(path.resolve()),
    )


def _failing_detector(path, **kwargs):
    raise RuntimeError("scene detector exploded")


def _audio_runner(command, **kwargs):
    Path(command[-1]).write_bytes(b"extracted-from-verified-media")
    return SimpleNamespace(returncode=0, stdout="", stderr="")


def _asr_runner(command, **kwargs):
    return SimpleNamespace(
        returncode=0,
        stdout=json.dumps(
            {"segments": [{"start": 0.0, "end": 1.5, "text": "hello"}], "language": "en"}
        ),
        stderr="",
    )


def test_extract_evidence_rejects_media_that_does_not_match_manifest_hash(tmp_path: Path):
    media = _write_media(tmp_path / "source.mp4")
    manifest = _manifest(media)
    swapped = _write_media(tmp_path / "swapped.mp4", b"different-media-bytes")

    with pytest.raises(ValueError, match="hash"):
        extract_evidence(
            manifest,
            swapped,
            tmp_path / "run-swapped",
            detector=_failing_detector,
            ocr_engine="none",
        )


def test_extract_evidence_rejects_changed_reference_media(tmp_path: Path):
    media = _write_media(tmp_path / "source.mp4")
    manifest = _manifest(media)
    media.write_bytes(b"changed-after-ingest")

    with pytest.raises(ValueError, match="hash"):
        extract_evidence(
            manifest,
            run_root=tmp_path / "run-changed-ref",
            detector=_failing_detector,
            ocr_engine="none",
        )


def test_asr_from_another_source_becomes_explicit_failure(tmp_path: Path):
    media = _write_media(tmp_path / "source.mp4")
    manifest = _manifest(media)
    audio = tmp_path / "other.wav"
    audio.write_bytes(b"other-source-audio")
    request = ASRRequest(request_id="asr-1", source_id="src-other", audio_path=audio)

    def runner(command, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"segments": [{"start": 0.0, "end": 1.0, "text": "nope"}]}),
            stderr="",
        )

    bundle = extract_evidence(
        manifest,
        media,
        tmp_path / "run-asr-source",
        asr_request=request,
        asr_runner=runner,
        audio_runner=_audio_runner,
        detector=_failing_detector,
        ocr_engine="none",
    )
    assert bundle.transcript.segments == []
    assert bundle.transcript.audio_input_hash is None
    assert any("source_id" in gap.reason for gap in bundle.transcript.gaps)


def test_asr_records_extract_transform_from_verified_media(tmp_path: Path):
    media = _write_media(tmp_path / "source.mp4")
    manifest = _manifest(media)
    request = ASRRequest(
        request_id="asr-1",
        source_id=manifest.source_id,
        audio_path=tmp_path / "unused-placeholder.wav",
    )
    run_root = tmp_path / "run-asr-bound"
    bundle = extract_evidence(
        manifest,
        media,
        run_root,
        asr_request=request,
        asr_runner=_asr_runner,
        audio_runner=_audio_runner,
        detector=_failing_detector,
        ocr_engine="none",
    )
    extracted = run_root / "tmp/asr-audio.wav"
    assert extracted.is_file()
    assert bundle.transcript.segments
    assert bundle.transcript.audio_input_hash == content_sha256(extracted)
    assert bundle.transcript.audio_parent_hash == manifest.sha256
    assert bundle.transcript.audio_command_digest
    assert bundle.transcript.time_offset_seconds == 0.0


def test_arbitrary_external_wav_is_rejected_as_unrelated_asr_audio(tmp_path: Path):
    media = _write_media(tmp_path / "source.mp4")
    manifest = _manifest(media)
    external = tmp_path / "external.wav"
    external.write_bytes(b"arbitrary-external-wav")
    request = ASRRequest(
        request_id="asr-1",
        source_id=manifest.source_id,
        audio_path=external,
    )
    bundle = extract_evidence(
        manifest,
        media,
        tmp_path / "run-asr-unrelated",
        asr_request=request,
        asr_runner=_asr_runner,
        audio_runner=_audio_runner,
        detector=_failing_detector,
        ocr_engine="none",
    )
    assert bundle.transcript.segments == []
    assert bundle.transcript.audio_input_hash is None
    assert any(
        "not derived" in gap.reason or "unrelated" in gap.reason
        for gap in bundle.transcript.gaps
    )


def test_asr_result_requires_audio_provenance_and_matching_source():
    result = ASRResult(
        request_id="asr-1",
        source_id="src-other",
        engine="whisperx",
        model="tiny",
        language="en",
        device="cpu",
        alignment="sentence",
        diarization=False,
        offset_seconds=0.0,
        status="complete",
        raw_artifact_hash="a" * 64,
        segments=[
            ASRSegment(
                id="asr-1",
                start_seconds=0.0,
                end_seconds=1.0,
                text_original="hello",
                language="en",
                words=[TranscriptWord(text="hello", start_seconds=0.0, end_seconds=1.0)],
                alignment_status="aligned",
            )
        ],
    )
    with pytest.raises(ASRError, match="source_id"):
        _transcript_from_asr(result, source_id="src-test", duration=2.0)

    matching = result.model_copy(update={"source_id": "src-test"})
    with pytest.raises(ASRError, match="audio input hash"):
        _transcript_from_asr(matching, source_id="src-test", duration=2.0)


def test_reference_media_mutation_during_extract_does_not_forge_provenance(tmp_path: Path):
    original = b"original-reference-bytes"
    media = _write_media(tmp_path / "source.mp4", original)
    original_hash = content_sha256(media)
    manifest = _manifest(media)
    run_root = tmp_path / "run-ref-mutate"

    def audio_runner(command, **kwargs):
        media.write_bytes(b"mutated-during-ffmpeg")
        Path(command[-1]).write_bytes(b"extracted-from-verified-media")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def visual_runner(command, **kwargs):
        media.write_bytes(b"mutated-during-frame-extract")
        from PIL import Image

        Image.new("RGB", (80, 40), "white").save(command[-1])
        return SimpleNamespace(
            returncode=0,
            stdout="",
            stderr="[Parsed_showinfo_0] n:1 pts_time:0.200000 duration:0.1",
        )

    request = ASRRequest(
        request_id="asr-1",
        source_id=manifest.source_id,
        audio_path=tmp_path / "unused-placeholder.wav",
    )
    bundle = extract_evidence(
        manifest,
        media,
        run_root,
        asr_request=request,
        asr_runner=_asr_runner,
        audio_runner=audio_runner,
        detector=lambda path, **kwargs: [(0.0, 2.0)],
        runner=visual_runner,
        ocr_engine="none",
    )
    snapshot = run_root / "media" / f"{original_hash[:16]}.mp4"
    assert snapshot.is_file()
    assert content_sha256(snapshot) == original_hash
    assert content_sha256(media) != original_hash
    assert bundle.source_hash == original_hash
    assert bundle.transcript.audio_parent_hash == original_hash
    assert bundle.transcript.segments


def test_working_media_change_during_audio_extract_discards_output(tmp_path: Path):
    media = _write_media(tmp_path / "source.mp4", b"stable-parent-bytes")
    output = tmp_path / "out.wav"

    def mutating_runner(command, **kwargs):
        Path(command[command.index("-i") + 1]).write_bytes(b"changed-mid-extract")
        Path(command[-1]).write_bytes(b"forged-audio")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with pytest.raises(ASRError, match="changed"):
        extract_audio_with_provenance(media, output, runner=mutating_runner)
    assert not output.exists()


def test_explicit_none_ocr_skips_regions_and_records_unavailable_gap(tmp_path: Path):
    media = _write_media(tmp_path / "source.mp4")
    manifest = _manifest(media)
    run_root = tmp_path / "run-no-ocr"

    def visual_runner(command, **kwargs):
        from PIL import Image

        Image.new("RGB", (80, 40), "white").save(command[-1])
        return SimpleNamespace(
            returncode=0,
            stdout="",
            stderr="[Parsed_showinfo_0] n:1 pts_time:0.200000 duration:0.1",
        )

    bundle = extract_evidence(
        manifest,
        media,
        run_root,
        detector=lambda path, **kwargs: [(0.0, 2.0)],
        runner=visual_runner,
        ocr_engine="none",
        ocr_unavailable_reason="OCR engine disabled by configuration",
    )
    assert bundle.visual.ocr_regions == []
    assert bundle.visual.status == "degraded"
    assert any(
        gap.modality == "ocr" and "disabled by configuration" in gap.reason for gap in bundle.gaps
    )


def _tsuki_ocr_runner(command, **kwargs):
    return SimpleNamespace(
        returncode=0,
        stdout=json.dumps(
            {
                "engine": "fake-ocr",
                "regions": [
                    {
                        "text": "文法 ～につき",
                        "bbox": {"x": 1, "y": 1, "width": 40, "height": 12},
                        "confidence": 0.92,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        stderr="",
    )


def _nigatsu_asr_runner(command, **kwargs):
    return SimpleNamespace(
        returncode=0,
        stdout=json.dumps(
            {
                "segments": [
                    {
                        "start": 0.0,
                        "end": 1.5,
                        "text": "今日は2月の文法を説明します。",
                    }
                ],
                "language": "ja",
            }
        ),
        stderr="",
    )


def test_extract_evidence_ocr_regions_enable_transcript_visual_asr_correction(tmp_path: Path):
    media = _write_media(tmp_path / "source.mp4")
    manifest = _manifest(media)
    run_root = tmp_path / "run-ocr-asr-fix"
    request = ASRRequest(
        request_id="asr-1",
        source_id=manifest.source_id,
        audio_path=tmp_path / "unused-placeholder.wav",
    )

    def visual_runner(command, **kwargs):
        from PIL import Image

        Image.new("RGB", (80, 40), "white").save(command[-1])
        return SimpleNamespace(
            returncode=0,
            stdout="",
            stderr="[Parsed_showinfo_0] n:1 pts_time:0.200000 duration:0.1",
        )

    bundle = extract_evidence(
        manifest,
        media,
        run_root,
        asr_request=request,
        asr_runner=_nigatsu_asr_runner,
        audio_runner=_audio_runner,
        detector=lambda path, **kwargs: [(0.0, 2.0)],
        runner=visual_runner,
        ocr_engine="fake",
        ocr_runner=_tsuki_ocr_runner,
    )
    assert bundle.visual.ocr_regions
    assert bundle.transcript.segments
    assert "2月" not in bundle.transcript.segments[0].text_original
    assert "～につき" in bundle.transcript.segments[0].text_original
    assert "visual-asr-corrected" in bundle.transcript.segments[0].quality_flags


def test_scene_detector_failure_on_fresh_run_root_writes_failure_output(tmp_path: Path):
    media = _write_media(tmp_path / "source.mp4")
    manifest = _manifest(media)
    run_root = tmp_path / "brand-new-run"
    assert not run_root.exists()

    bundle = extract_evidence(
        manifest,
        media,
        run_root,
        detector=_failing_detector,
        ocr_engine="none",
    )

    visual_path = run_root / "evidence/visual-catalogue.json"
    assert visual_path.is_file()
    reloaded = VisualCatalogue.model_validate_json(visual_path.read_text(encoding="utf-8"))
    assert reloaded.status == "failed"
    assert reloaded.occurrences == []
    assert any(gap.modality == "visual" for gap in bundle.gaps)
    assert any("visual failure" in gap.reason for gap in bundle.visual.gaps)

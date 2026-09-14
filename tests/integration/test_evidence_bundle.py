from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from yt2class.adapters.ffmpeg import probe_media
from yt2class.adapters.asr import extract_audio
from yt2class.adapters.process import ProcessCancelled
from yt2class.adapters.scenes import FrameSample, extract_frame_with_timestamp
from yt2class.domain.evidence import EvidenceBundle
from yt2class.domain.source import SourceManifest, content_sha256
from yt2class.domain.visual import VisualCatalogue
from yt2class.orchestration.workspace import Workspace, WorkspacePathError
from yt2class.stages.extract_evidence import EvidenceCancelled, extract_evidence


pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe unavailable for local evidence integration",
)


def make_media(tmp_path: Path) -> tuple[Path, SourceManifest]:
    media = tmp_path / "synthetic lesson.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=96x64:r=10:d=1",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=96x64:r=10:d=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=16000:duration=2",
            "-filter_complex",
            "[0:v][1:v]concat=n=2:v=1:a=0[v]",
            "-map",
            "[v]",
            "-map",
            "2:a:0",
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(media),
        ],
        check=True,
    )
    probe = probe_media(media)
    manifest = SourceManifest(
        schema_version="1.0",
        source_id="src-evidence",
        kind="local",
        title="Synthetic lesson",
        media_path="media/source.mp4",
        sha256=content_sha256(media),
        duration_seconds=probe.duration_seconds,
        streams=list(probe.streams),
        timebase=probe.timebase,
        local_mode="reference",
        reference_path=str(media.resolve()),
    )
    return media, manifest


def write_subtitles(tmp_path: Path) -> Path:
    subtitle = tmp_path / "lesson.ja.vtt"
    subtitle.write_text(
        """WEBVTT

00:00:00.100 --> 00:00:00.700
青い画面です。

00:00:01.200 --> 00:00:01.950
次の説明。
""",
        encoding="utf-8",
    )
    return subtitle


def test_extract_evidence_builds_stable_transcript_visual_and_gaps(tmp_path: Path):
    media, manifest = make_media(tmp_path)
    subtitle = write_subtitles(tmp_path)
    run_root = tmp_path / "run"

    bundle = extract_evidence(
        manifest,
        media,
        run_root,
        sidecar=subtitle,
        profile="content",
        detector=lambda path, **kwargs: [(0.0, manifest.duration_seconds)],
        ocr_engine="none",
    )

    assert isinstance(bundle, EvidenceBundle)
    assert bundle.source_id == manifest.source_id
    assert bundle.transcript.segments
    assert bundle.visual.scenes
    assert bundle.visual.occurrences
    assert bundle.status == "degraded"  # OCR is optional and explicitly unavailable.
    assert any(g.modality == "ocr" for g in bundle.gaps)
    assert (run_root / "evidence/transcript-document.json").is_file()
    assert (run_root / "evidence/visual-catalogue.json").is_file()
    assert (run_root / "evidence/evidence-bundle.json").is_file()

    first_bytes = (run_root / "evidence/evidence-bundle.json").read_bytes()
    second = extract_evidence(
        manifest,
        media,
        run_root,
        sidecar=subtitle,
        profile="content",
        detector=lambda path, **kwargs: [(0.0, manifest.duration_seconds)],
        ocr_engine="none",
    )
    assert second.model_dump(mode="json") == bundle.model_dump(mode="json")
    assert (run_root / "evidence/evidence-bundle.json").read_bytes() == first_bytes

    for asset in bundle.visual.assets:
        frame = run_root / asset.path
        assert frame.is_file()
        assert hashlib.sha256(frame.read_bytes()).hexdigest() == asset.sha256


def test_generated_media_can_be_prepared_for_asr(tmp_path: Path):
    media, _manifest = make_media(tmp_path)
    audio = extract_audio(media, tmp_path / "audio.wav")
    details = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=sample_rate,channels",
            "-of",
            "json",
            str(audio),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    stream = json.loads(details.stdout)["streams"][0]
    assert stream["sample_rate"] == "16000"
    assert stream["channels"] == 1


def test_real_ffmpeg_reports_decoded_pts_separately_from_request(tmp_path: Path):
    media, _manifest = make_media(tmp_path)
    frame_times = [
        float(frame["best_effort_timestamp_time"])
        for frame in json.loads(
            subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "frame=best_effort_timestamp_time",
                    "-of",
                    "json",
                    str(media),
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        )["frames"]
    ]
    for index, requested in enumerate((0.237, 0.61), start=1):
        extraction = extract_frame_with_timestamp(
            media,
            FrameSample(id=f"sample-pts-{index}", scene_id="scene-1", requested_seconds=requested),
            tmp_path / f"decoded-{index}.jpg",
        )
        assert extraction.actual_source_seconds is not None
        assert extraction.actual_source_seconds >= requested
        assert any(
            extraction.actual_source_seconds == pytest.approx(frame_time, abs=0.001)
            for frame_time in frame_times
        )
        assert extraction.path.is_file()


def test_extract_evidence_keeps_transcript_when_frame_side_fails(tmp_path: Path):
    media, manifest = make_media(tmp_path)
    subtitle = write_subtitles(tmp_path)
    run_root = tmp_path / "run"

    def failed_frame_runner(command, **kwargs):
        return type("Completed", (), {"returncode": 1, "stdout": "", "stderr": "decode failed"})()

    bundle = extract_evidence(
        manifest,
        media,
        run_root,
        sidecar=subtitle,
        detector=lambda path, **kwargs: [(0.0, manifest.duration_seconds)],
        runner=failed_frame_runner,
        ocr_engine="none",
    )

    assert bundle.transcript.segments
    assert bundle.visual.occurrences == []
    assert any(g.modality == "visual" for g in bundle.gaps)


def test_extract_evidence_binds_optional_ocr_to_each_occurrence(tmp_path: Path):
    media, manifest = make_media(tmp_path)
    subtitle = write_subtitles(tmp_path)
    run_root = tmp_path / "run"

    def ocr_runner(command, **kwargs):
        return type(
            "Completed",
            (),
            {
                "returncode": 0,
                "stdout": json.dumps(
                    {
                        "engine": "fake-ocr",
                        "regions": [
                            {
                                "text": "标题",
                                "bbox": {"x": 1, "y": 1, "width": 20, "height": 8},
                                "confidence": 0.9,
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                "stderr": "",
            },
        )()

    bundle = extract_evidence(
        manifest,
        media,
        run_root,
        sidecar=subtitle,
        detector=lambda path, **kwargs: [(0.0, manifest.duration_seconds)],
        ocr_engine="fake",
        ocr_runner=ocr_runner,
    )
    assert len(bundle.visual.ocr_regions) == len(bundle.visual.occurrences)
    assert len({region.id for region in bundle.visual.ocr_regions}) == len(bundle.visual.ocr_regions)
    assert all(occurrence.quality.ocr_density > 0 for occurrence in bundle.visual.occurrences)
    assert not any(g.modality == "ocr" for g in bundle.gaps)


def test_out_of_bounds_ocr_box_becomes_gap_and_reload_stays_valid(tmp_path: Path):
    media, manifest = make_media(tmp_path)
    subtitle = write_subtitles(tmp_path)
    run_root = tmp_path / "run-ocr-oob"

    def ocr_runner(command, **kwargs):
        return type(
            "Completed",
            (),
            {
                "returncode": 0,
                "stdout": json.dumps(
                    {
                        "engine": "fake-ocr",
                        "regions": [
                            {
                                "text": "越界",
                                "bbox": {"x": 80, "y": 1, "width": 40, "height": 8},
                                "confidence": 0.9,
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                "stderr": "",
            },
        )()

    bundle = extract_evidence(
        manifest,
        media,
        run_root,
        sidecar=subtitle,
        detector=lambda path, **kwargs: [(0.0, manifest.duration_seconds)],
        ocr_engine="fake",
        ocr_runner=ocr_runner,
    )
    assert bundle.visual.ocr_regions == []
    assert any(gap.modality == "ocr" for gap in bundle.gaps)
    assert bundle.visual.status != "complete"
    reloaded = VisualCatalogue.model_validate_json(
        (run_root / "evidence/visual-catalogue.json").read_text(encoding="utf-8")
    )
    assert reloaded.ocr_regions == []
    EvidenceBundle.model_validate_json(
        (run_root / "evidence/evidence-bundle.json").read_text(encoding="utf-8")
    )


def test_extract_evidence_without_subtitles_reports_transcript_gap(tmp_path: Path):
    media, manifest = make_media(tmp_path)
    run_root = tmp_path / "run"
    bundle = extract_evidence(
        manifest,
        media,
        run_root,
        detector=lambda path, **kwargs: [(0.0, manifest.duration_seconds)],
        ocr_engine="none",
    )
    assert bundle.transcript.segments == []
    assert bundle.transcript.raw_artifact_hash is None
    assert any(g.modality == "transcript" for g in bundle.gaps)


def test_extract_evidence_propagates_asr_cancellation(tmp_path: Path):
    media, manifest = make_media(tmp_path)

    def cancelled_runner(command, **kwargs):
        raise ProcessCancelled("cancelled ASR")

    from yt2class.adapters.asr import ASRRequest

    request = ASRRequest(
        request_id="asr-cancel",
        source_id=manifest.source_id,
        audio_path=media,
    )
    with pytest.raises(EvidenceCancelled, match="ASR"):
        extract_evidence(
            manifest,
            media,
            tmp_path / "run-asr-cancel",
            asr_request=request,
            asr_runner=cancelled_runner,
        )


def test_extract_evidence_propagates_frame_cancellation(tmp_path: Path):
    media, manifest = make_media(tmp_path)

    def cancelled_runner(command, **kwargs):
        raise ProcessCancelled("cancelled frame")

    with pytest.raises(EvidenceCancelled, match="frame"):
        extract_evidence(
            manifest,
            media,
            tmp_path / "run-frame-cancel",
            sidecar=write_subtitles(tmp_path),
            detector=lambda path, **kwargs: [(0.0, manifest.duration_seconds)],
            runner=cancelled_runner,
        )


def test_extract_evidence_propagates_ocr_cancellation(tmp_path: Path):
    media, manifest = make_media(tmp_path)

    def cancelled_ocr_runner(command, **kwargs):
        raise ProcessCancelled("cancelled OCR")

    with pytest.raises(EvidenceCancelled, match="OCR"):
        extract_evidence(
            manifest,
            media,
            tmp_path / "run-ocr-cancel",
            sidecar=write_subtitles(tmp_path),
            detector=lambda path, **kwargs: [(0.0, manifest.duration_seconds)],
            ocr_engine="fake",
            ocr_runner=cancelled_ocr_runner,
        )


def test_extract_evidence_uses_workspace_lock_and_safe_paths(tmp_path: Path):
    media, manifest = make_media(tmp_path)
    workspace = Workspace.create(tmp_path / "runs", run_id="evidence-workspace")
    with workspace.write_lock():
        bundle = extract_evidence(
            manifest,
            media,
            workspace=workspace,
            lock=False,
            sidecar=write_subtitles(tmp_path),
            detector=lambda path, **kwargs: [(0.0, manifest.duration_seconds)],
            ocr_engine="none",
        )
    assert bundle.source_id == manifest.source_id
    assert (workspace.root / "evidence/evidence-bundle.json").is_file()


def test_extract_evidence_rejects_workspace_evidence_symlink_escape(tmp_path: Path):
    media, manifest = make_media(tmp_path)
    workspace = Workspace.create(tmp_path / "runs", run_id="evidence-symlink")
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace.root / "evidence").symlink_to(outside, target_is_directory=True)

    with pytest.raises(WorkspacePathError, match="outside workspace"):
        extract_evidence(
            manifest,
            media,
            workspace=workspace,
            sidecar=write_subtitles(tmp_path),
            detector=lambda path, **kwargs: [(0.0, manifest.duration_seconds)],
            ocr_engine="none",
        )
    assert not list(outside.glob("*.json"))


def test_youtube_ingest_auto_caption_reaches_evidence_without_sidecar(tmp_path):
    from types import SimpleNamespace
    from yt2class.adapters.ytdlp import download_video
    from yt2class.stages.ingest import ingest_source

    original, _ = make_media(tmp_path)
    workspace = Workspace.create(tmp_path / "runs", run_id="auto-captions")
    media = workspace.media_dir / "lesson [fixture-id].mp4"

    def runner(command, **kwargs):
        if "--skip-download" in command:
            assert command[command.index("--sub-langs") + 1] == "ja"
            shutil.copyfile(write_subtitles(tmp_path), media.with_suffix(".ja.vtt"))
        else:
            shutil.copyfile(original, media)
            media.with_suffix(".info.json").write_text(json.dumps({
                "id": "fixture-id", "title": "Japanese lesson", "language": "ja",
                "automatic_captions": {"ja": [{"ext": "vtt"}]}, "subtitles": {},
            }))
        return SimpleNamespace(returncode=0, stdout=str(media), stderr="")

    result = ingest_source("https://youtu.be/fixture-id", workspace,
                          downloader=lambda source, directory: download_video(source, directory, runner=runner))
    bundle = extract_evidence(result.manifest, workspace=workspace, profile="content",
                              detector=lambda path, **kwargs: [(0.0, result.manifest.duration_seconds)],
                              ocr_engine="none")
    assert bundle.transcript.segments
    assert bundle.transcript.language == "ja"
    assert all(segment.origin == "auto-caption" for segment in bundle.transcript.segments)
    assert not any(gap.reason == "no subtitle or ASR evidence" for gap in bundle.gaps)

    # An explicit sidecar remains authoritative even if a downloaded caption was removed.
    media.with_suffix(".ja.vtt").unlink()
    override = extract_evidence(result.manifest, workspace=workspace, sidecar=write_subtitles(tmp_path),
                                profile="content", detector=lambda path, **kwargs: [(0.0, result.manifest.duration_seconds)],
                                ocr_engine="none")
    assert override.transcript.segments
    assert all(segment.origin == "sidecar" for segment in override.transcript.segments)

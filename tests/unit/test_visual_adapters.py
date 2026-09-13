from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from yt2class.adapters.scenes import (
    FrameSample,
    SceneCancelled,
    SceneRange,
    build_frame_command,
    detect_scene_ranges,
    extract_visual_catalogue,
    extract_frame,
    measure_frame_quality,
    sample_scene_times,
)
from yt2class.domain.visual import (
    FrameOccurrence,
    FrameQuality,
    OcrRegion,
    Scene,
    VisualAsset,
    VisualCatalogue,
)


def test_scene_sampling_uses_multiple_points_and_supplements_long_static_ranges():
    scenes = [SceneRange(id="scene-1", start_seconds=0.0, end_seconds=120.0, detector="content")]
    samples = sample_scene_times(scenes, duration_seconds=120.0, static_interval_seconds=30.0)
    timestamps = [sample.requested_seconds for sample in samples]

    assert 24.0 in timestamps
    assert 60.0 in timestamps
    assert 96.0 in timestamps
    assert 30.0 in timestamps
    assert 90.0 in timestamps
    assert len(timestamps) >= 5


def test_scene_detection_supports_adaptive_profile_with_bounded_ranges(tmp_path: Path):
    seen = {}

    def detector(path, **kwargs):
        seen["profile"] = kwargs["profile"]
        return [(0.0, 0.5), (0.5, 1.0)]

    scenes = detect_scene_ranges(
        tmp_path / "source.mp4",
        duration_seconds=1.0,
        profile="adaptive",
        detector=detector,
    )
    assert seen["profile"] == "adaptive"
    assert [(scene.start_seconds, scene.end_seconds) for scene in scenes] == [
        (0.0, 0.5),
        (0.5, 1.0),
    ]


def test_scene_detection_normalizes_order_overlap_duplicates_and_internal_gaps(tmp_path: Path):
    scenes = detect_scene_ranges(
        tmp_path / "source.mp4",
        duration_seconds=2.0,
        detector=lambda path, **kwargs: [
            (1.5, 2.0),
            (0.4, 1.0),
            (0.4, 1.0),
            (0.8, 1.6),
        ],
    )
    assert [(scene.start_seconds, scene.end_seconds) for scene in scenes] == [
        (0.0, 0.4),
        (0.4, 1.0),
        (1.0, 1.6),
        (1.6, 2.0),
    ]
    assert all(left.end_seconds == right.start_seconds for left, right in zip(scenes, scenes[1:]))


def test_frame_command_is_shell_free_and_keeps_requested_timestamp(tmp_path: Path):
    command = build_frame_command(
        tmp_path / "source.mkv",
        requested_seconds=12.5,
        output_path=tmp_path / "frames" / "frame.jpg",
    )
    assert command[0] == "ffmpeg"
    assert "-ss" in command
    assert command[command.index("-ss") + 1] == "12.500000"
    assert "-copyts" in command
    assert "-start_at_zero" in command
    assert command[command.index("-vf") + 1] == "showinfo"
    assert "-frames:v" in command
    assert "--" not in command


def test_frame_quality_reports_dimensions_brightness_sharpness_and_ocr_density(tmp_path: Path):
    image_path = tmp_path / "frame.png"
    image = Image.new("RGB", (80, 40), "#707070")
    ImageDraw.Draw(image).rectangle((10, 10, 70, 30), fill="white")
    image.save(image_path)

    quality = measure_frame_quality(image_path, ocr_text="画面文字")
    assert quality.width == 80
    assert quality.height == 40
    assert 0.0 <= quality.brightness <= 1.0
    assert 0.0 <= quality.sharpness <= 1.0
    assert 0.0 < quality.ocr_density <= 1.0


def test_visual_catalogue_keeps_dark_frame_occurrence_with_reject_reason(tmp_path: Path):
    def runner(command, **kwargs):
        Image.new("RGB", (80, 40), "black").save(command[-1])
        return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    catalogue = extract_visual_catalogue(
        source_id="src-dark",
        video_path=tmp_path / "source.mp4",
        run_root=tmp_path / "run",
        duration_seconds=1.0,
        detector=lambda path, **kwargs: [(0.0, 1.0)],
        runner=runner,
    )
    assert catalogue.occurrences
    assert catalogue.occurrences[0].reject_reason == "near-black/low-variation"
    assert catalogue.status == "degraded"
    assert catalogue.gaps
    assert any("no accepted visual occurrence" in gap.reason for gap in catalogue.gaps)


def test_visual_catalogue_does_not_claim_requested_time_is_decoded_pts(tmp_path: Path):
    def runner(command, **kwargs):
        Image.new("RGB", (80, 40), "white").save(command[-1])
        return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    catalogue = extract_visual_catalogue(
        source_id="src-no-pts",
        video_path=tmp_path / "source.mp4",
        run_root=tmp_path / "run",
        duration_seconds=1.0,
        detector=lambda path, **kwargs: [(0.0, 1.0)],
        runner=runner,
    )
    occurrence = catalogue.occurrences[0]
    assert occurrence.requested_seconds != 0.0
    assert occurrence.timestamp_seconds is None
    assert occurrence.actual_source_seconds is None
    assert "actual-timestamp-unavailable" in occurrence.quality_flags
    assert catalogue.status == "degraded"


def test_frame_cancellation_is_not_converted_to_a_visual_gap(tmp_path: Path):
    from yt2class.adapters.process import ProcessCancelled

    def runner(command, **kwargs):
        raise ProcessCancelled("cancelled frame")

    with pytest.raises(SceneCancelled, match="cancelled frame"):
        extract_frame(
            tmp_path / "source.mp4",
            FrameSample(id="sample-1", scene_id="scene-1", requested_seconds=0.2),
            tmp_path / "frame.jpg",
            runner=runner,
        )


def test_visual_catalogue_parses_decoded_pts_from_ffmpeg_showinfo(tmp_path: Path):
    def runner(command, **kwargs):
        Image.new("RGB", (80, 40), "white").save(command[-1])
        return type(
            "Completed",
            (),
            {
                "returncode": 0,
                "stdout": "",
                "stderr": "[Parsed_showinfo_0] n:1 pts_time:0.333333 duration:0.1",
            },
        )()

    catalogue = extract_visual_catalogue(
        source_id="src-with-pts",
        video_path=tmp_path / "source.mp4",
        run_root=tmp_path / "run",
        duration_seconds=1.0,
        detector=lambda path, **kwargs: [(0.0, 1.0)],
        runner=runner,
    )
    occurrence = catalogue.occurrences[0]
    assert occurrence.requested_seconds == 0.2
    assert occurrence.actual_source_seconds == pytest.approx(0.333333)
    assert occurrence.timestamp_seconds == pytest.approx(0.333333)


def _minimal_visual(*, role: str = "frame", occurrence_asset_id: str = "asset-1", region_asset_id: str | None = None, bbox=None):
    asset = VisualAsset(
        id="asset-1",
        role=role,
        path="frames/frame.jpg",
        sha256="a" * 64,
        mime_type="image/jpeg",
        width=100,
        height=50,
    )
    occurrence = FrameOccurrence(
        id="occ-1",
        scene_id="scene-1",
        asset_id=occurrence_asset_id,
        requested_seconds=0.5,
        timestamp_seconds=0.5,
        actual_source_seconds=0.5,
        quality=FrameQuality(
            width=100,
            height=50,
            brightness=0.5,
            sharpness=0.5,
            ocr_density=0.0,
        ),
    )
    region = OcrRegion(
        id="ocr-occ-1-0001",
        asset_id=region_asset_id or occurrence_asset_id,
        parent_occurrence_id="occ-1",
        bbox=bbox or {"x": 10, "y": 10, "width": 20, "height": 10},
        text="word",
        engine="test",
        confidence=0.9,
    )
    return VisualCatalogue(
        schema_version="1.0",
        source_id="src-visual",
        scenes=[Scene(id="scene-1", start_seconds=0.0, end_seconds=1.0, detector="content")],
        assets=[asset],
        occurrences=[occurrence],
        ocr_regions=[region],
    )


def test_visual_catalogue_rejects_non_frame_occurrence_asset():
    import pytest

    with pytest.raises(ValueError, match="role"):
        _minimal_visual(role="clip")


def test_visual_catalogue_rejects_ocr_asset_mismatch():
    import pytest

    with pytest.raises(ValueError, match="asset_id"):
        _minimal_visual(region_asset_id="asset-other")


def test_visual_catalogue_rejects_ocr_bbox_outside_asset():
    import pytest

    with pytest.raises(ValueError, match="bbox"):
        _minimal_visual(bbox={"x": 90, "y": 45, "width": 20, "height": 10})


def test_complete_visual_catalogue_rejects_uncovered_scene_union():
    import pytest

    with pytest.raises(ValueError, match="derived|scenes|uncovered|complete|occurrence"):
        VisualCatalogue(
            schema_version="1.0",
            source_id="src-visual",
            scenes=[Scene(id="scene-1", start_seconds=0.4, end_seconds=1.0, detector="content")],
            assets=[],
            occurrences=[],
            ocr_regions=[],
            status="complete",
        )


def test_complete_visual_rejects_scenes_without_occurrences():
    import pytest

    with pytest.raises(ValueError, match="occurrence|asset"):
        VisualCatalogue(
            schema_version="1.0",
            source_id="src-visual",
            scenes=[Scene(id="scene-1", start_seconds=0.0, end_seconds=1.0, detector="content")],
            assets=[],
            occurrences=[],
            ocr_regions=[],
            status="complete",
        )


def test_complete_visual_rejects_catalogue_with_only_rejected_occurrences():
    with pytest.raises(ValueError, match="occurrence|usable"):
        VisualCatalogue(
            schema_version="1.0",
            source_id="src-visual",
            scenes=[Scene(id="scene-1", start_seconds=0.0, end_seconds=60.0, detector="content")],
            assets=[
                VisualAsset(
                    id="asset-1",
                    role="frame",
                    path="frames/frame.jpg",
                    sha256="a" * 64,
                    mime_type="image/jpeg",
                    width=80,
                    height=40,
                )
            ],
            occurrences=[
                FrameOccurrence(
                    id="occ-1",
                    scene_id="scene-1",
                    asset_id="asset-1",
                    requested_seconds=0.2,
                    timestamp_seconds=0.2,
                    actual_source_seconds=0.2,
                    reject_reason="near-black/low-variation",
                    quality=FrameQuality(
                        width=80,
                        height=40,
                        brightness=0.0,
                        sharpness=0.0,
                        ocr_density=0.0,
                    ),
                )
            ],
            ocr_regions=[],
            status="complete",
        )

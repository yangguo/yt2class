from pathlib import Path
import inspect

from PIL import Image, ImageDraw

from yt2class import scenes


def make_fixture_frames(tmp_path: Path):
    first = tmp_path / "first.png"
    duplicate = tmp_path / "duplicate.png"
    changed = tmp_path / "changed.png"

    base = Image.new("RGB", (96, 54), "white")
    base.save(first)
    base.save(duplicate)

    board = Image.new("RGB", (96, 54), "white")
    ImageDraw.Draw(board).rectangle((8, 8, 88, 46), fill="black")
    board.save(changed)
    return (
        scenes.FrameCandidate("frame-1", 1.0, first),
        scenes.FrameCandidate("frame-2", 2.0, duplicate),
        scenes.FrameCandidate("frame-3", 3.0, changed),
    )


def test_deduplicate_keeps_first_of_visually_equivalent_frames(tmp_path: Path):
    assert hasattr(scenes, "FrameCandidate")
    first, duplicate, changed = make_fixture_frames(tmp_path)

    assert hasattr(scenes, "deduplicate_frames")
    kept = scenes.deduplicate_frames(
        [first, duplicate, changed], hamming_threshold=4
    )

    assert [item.path.name for item in kept] == ["first.png", "changed.png"]


def test_scene_detector_default_is_sensitive_to_courseware_changes():
    parameter = inspect.signature(scenes.detect_scene_timestamps).parameters["content_threshold"]
    assert parameter.default == 10.0


def test_unusable_near_black_frame_is_rejected(tmp_path: Path):
    black = tmp_path / "black.png"
    Image.new("RGB", (96, 54), "black").save(black)

    assert hasattr(scenes, "is_usable_frame")
    assert scenes.is_usable_frame(black) is False

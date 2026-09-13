from pathlib import Path

import pytest
from PIL import Image

from yt2class import renderer


def make_small_deck_spec(tmp_path: Path) -> dict[str, object]:
    frame = tmp_path / "frame.jpg"
    Image.new("RGB", (320, 180), "#d8effa").save(frame)
    return {
        "source_url": "https://www.youtube.com/watch?v=test",
        "title": "辞書形",
        "subtitle": "原始视频画面讲义",
        "slides": [
            {
                "frame_id": "frame-1",
                "timestamp": 12.5,
                "frame_path": str(frame),
                "kind": "grammar",
                "title": "重点画面",
                "explanation_zh": "保留原始板书画面。",
                "takeaway": "按顺序复习。",
            }
        ],
        "summary": ["保留原始画面", "按顺序复习", "回看时间点"],
        "quiz": [{"prompt": "请填空：____。", "answer": "回看原视频。"}],
    }


def test_setup_script_discovers_runtime_helper_without_machine_path(tmp_path, monkeypatch):
    helper = tmp_path / "setup_artifact_tool_workspace.mjs"
    helper.write_text("// fixture", encoding="utf-8")
    monkeypatch.delenv("YT2CLASS_ARTIFACT_SETUP", raising=False)
    monkeypatch.setattr(renderer, "_runtime_setup_candidates", lambda: [helper])

    assert renderer._setup_script() == helper


@pytest.mark.artifact_tool
def test_renderer_creates_pptx_that_references_original_frame(tmp_path: Path):
    assert hasattr(renderer, "render_deck")
    spec = make_small_deck_spec(tmp_path)

    output = renderer.render_deck(spec, tmp_path / "lesson.pptx")

    assert output.read_bytes()[:2] == b"PK"
    assert output.stat().st_size > 10_000


@pytest.mark.artifact_tool
def test_renderer_clears_stale_preview_slides(tmp_path: Path):
    spec = make_small_deck_spec(tmp_path)
    preview_dir = tmp_path / "previews"
    preview_dir.mkdir()
    stale = preview_dir / "slide-99.png"
    stale.write_bytes(b"stale")

    renderer.render_deck(spec, tmp_path / "lesson.pptx", preview_dir=preview_dir)

    assert not stale.exists()
    assert len(list(preview_dir.glob("slide-*.png"))) == 4

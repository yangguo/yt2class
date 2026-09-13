import json
from pathlib import Path

import pytest
from PIL import Image

from yt2class import pipeline, scenes


@pytest.mark.artifact_tool
def test_pipeline_with_reviewed_selection_writes_manifest_and_deck(tmp_path: Path, monkeypatch):
    assert hasattr(pipeline, "build_lesson")

    source = tmp_path / "source.mp4"
    source.write_bytes(b"fixture source")
    frame_path = tmp_path / "frame.jpg"
    Image.new("RGB", (320, 180), "#d8effa").save(frame_path)
    candidate = scenes.FrameCandidate("frame-1", 12.5, frame_path, "abc123")
    selection_file = tmp_path / "selection.json"
    selection_file.write_text(
        json.dumps(
            {
                "title": "辞書形",
                "subtitle": "原始视频画面讲义",
                "slides": [
                    {
                        "frame_id": "frame-1",
                        "kind": "grammar",
                        "title": "重点",
                        "explanation_zh": "保留原始板书画面。",
                        "takeaway": "按顺序复习。",
                    }
                ],
                "summary": ["重点", "顺序", "回看"],
                "quiz": [{"prompt": "填空", "answer": "答案"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(pipeline, "fetch_video_title", lambda url: "辞書形")
    monkeypatch.setattr(pipeline, "download_lesson", lambda url, media_dir: source)
    monkeypatch.setattr(
        pipeline,
        "extract_scene_candidates",
        lambda video_path, frames_dir: [candidate],
    )

    result = pipeline.build_lesson(
        "https://www.youtube.com/watch?v=test",
        tmp_path / "output",
        selection_file=selection_file,
        max_slides=1,
    )

    assert result.pptx_path.exists()
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["slides"][0]["source_frame_sha256"] == "abc123"
    source_notes = result.run_dir / "analysis" / "source-notes.txt"
    assert source_notes.exists()
    notes = source_notes.read_text(encoding="utf-8")
    assert "https://www.youtube.com/watch?v=test" in notes
    assert "frame-1" in notes
    assert manifest["source_notes"] == str(source_notes)

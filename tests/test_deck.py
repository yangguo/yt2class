from pathlib import Path

from yt2class import deck, lesson_plan, scenes


def test_build_deck_spec_binds_plan_slides_to_frame_provenance(tmp_path: Path):
    frame = scenes.FrameCandidate("frame-1", 12.5, tmp_path / "frame.jpg", "abc123")
    plan = lesson_plan.validate_plan(
        {
            "title": "辞書形",
            "slides": [
                {
                    "frame_id": "frame-1",
                    "kind": "grammar",
                    "title": "重点",
                    "explanation_zh": "保留原始画面。",
                }
            ],
            "summary": ["重点"],
            "quiz": [{"prompt": "填空", "answer": "答案"}],
        },
        {"frame-1"},
    )

    assert hasattr(deck, "build_deck_spec")
    spec = deck.build_deck_spec(
        "https://www.youtube.com/watch?v=test",
        plan,
        [frame],
    )

    assert spec["slides"][0]["frame_path"] == str(frame.path)
    assert spec["slides"][0]["source_sha256"] == "abc123"

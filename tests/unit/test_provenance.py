from __future__ import annotations

from pathlib import Path

import pytest

from yt2class.domain.source import SourceManifest
from yt2class.provenance import build_provenance, canonical_youtube_seek, precise_time_note
from yt2class.stages.bind_spec import bind_editorial_plan
from tests.helpers.m4 import seed_lecture_run


@pytest.mark.parametrize(
    ("url", "seconds", "expected"),
    [
        ("https://www.youtube.com/watch?v=abc123_", 0, "https://www.youtube.com/watch?v=abc123_&t=0s"),
        ("https://www.youtube.com/watch?v=abc123_&t=99s", 12.7, "https://www.youtube.com/watch?v=abc123_&t=12s"),
        ("https://youtu.be/abc123_", 3.9, "https://www.youtube.com/watch?v=abc123_&t=3s"),
        (None, 4.5, "t=4.5s"),
    ],
)
def test_canonical_youtube_seek(url, seconds, expected):
    assert canonical_youtube_seek(url, seconds) == expected


def test_precise_time_note_keeps_fractional_seconds():
    assert precise_time_note(12.345) == "source_time=12.345s"


def test_build_provenance_frame_seek_uses_asset_timestamps(tmp_path: Path):
    workspace, outcome, source, transcript, visual, _ = seed_lecture_run(tmp_path)
    youtube = source.model_copy(
        update={
            "kind": "youtube",
            "url": "https://www.youtube.com/watch?v=abc123_",
            "video_id": "abc123_",
        }
    )
    bound = bind_editorial_plan(
        plan=outcome.plan,
        knowledge=outcome.knowledge,
        report=outcome.report,
        source=youtube,
        transcript=transcript,
        visual=visual,
        workspace=workspace,
    )
    result = build_provenance(bound.spec, workspace=workspace)
    image_slide = next(
        s for s in bound.spec.slides if s.type == "content" and s.layout == "image-text"
    )
    asset_id = image_slide.frame_asset_ids[0]
    expected_ts = next(a.timestamp_seconds for a in bound.spec.assets if a.id == asset_id)
    page_row = next(p for p in result.payload["pages"] if p["page_id"] == image_slide.id)
    frame_citation = next(c for c in page_row["citations"] if c.get("asset_id") == asset_id)
    assert f"t={int(expected_ts)}s" in frame_citation["seek_url"]
    assert frame_citation["seconds"] == expected_ts

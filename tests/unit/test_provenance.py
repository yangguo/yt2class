from __future__ import annotations

from pathlib import Path

import pytest

from yt2class.provenance import canonical_youtube_seek, precise_time_note
from yt2class.provenance import build_provenance
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


def test_build_provenance_writes_sources_json(tmp_path: Path):
    workspace, outcome, source, transcript, visual, _ = seed_lecture_run(tmp_path)
    bound = bind_editorial_plan(
        plan=outcome.plan,
        knowledge=outcome.knowledge,
        report=outcome.report,
        source=source,
        transcript=transcript,
        visual=visual,
        workspace=workspace,
    )
    result = build_provenance(bound.spec, workspace=workspace)
    path = workspace.safe_path(result.sources_path)
    assert path.is_file()
    assert result.payload["pages"]
    local = next(page for page in result.payload["pages"] if page["citations"])
    assert any("sha256" in str(row) or "seek_url" in str(row) for row in local["citations"])

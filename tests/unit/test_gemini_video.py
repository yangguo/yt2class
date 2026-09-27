from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import httpx

from yt2class.gemini_video import (
    GeminiPreviewError,
    expand_video_inputs,
    generate_video_preview,
    write_video_preview,
)


def test_expands_playlist_and_deduplicates_videos_in_input_order(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(
            stdout=json.dumps(
                {
                    "entries": [
                        {"id": "first", "title": "First"},
                        {"id": "second", "title": "Second"},
                    ]
                }
            ),
            stderr="",
        )

    monkeypatch.setattr("yt2class.gemini_video.subprocess.run", run)

    entries = expand_video_inputs(
        ["https://youtu.be/first", "https://www.youtube.com/playlist?list=PL123"]
    )

    assert [entry.url for entry in entries] == [
        "https://www.youtube.com/watch?v=first",
        "https://www.youtube.com/watch?v=second",
    ]
    assert entries[1].title == "Second"
    assert calls[0][0] == "yt-dlp"
    assert "--flat-playlist" in calls[0]
    assert "--skip-download" in calls[0]


def test_playlist_expansion_failure_is_reported_without_shell(monkeypatch):
    def run(command, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr("yt2class.gemini_video.subprocess.run", run)

    with pytest.raises(ValueError, match="yt-dlp"):
        expand_video_inputs(["https://www.youtube.com/playlist?list=PL123"])


def test_rejects_non_youtube_playlist_url_without_running_ytdlp(monkeypatch):
    monkeypatch.setattr(
        "yt2class.gemini_video.subprocess.run",
        lambda *args, **kwargs: pytest.fail("must not invoke yt-dlp for another host"),
    )
    with pytest.raises(ValueError, match="only YouTube"):
        expand_video_inputs(["https://example.com/playlist?list=PL123"])


def test_rejects_unsafe_model_name():
    with httpx.Client(transport=httpx.MockTransport(lambda _request: pytest.fail("no request"))) as client:
        with pytest.raises(GeminiPreviewError, match="Invalid Gemini model"):
            generate_video_preview("https://youtu.be/vid", api_key="test-key", client=client, model="x?key=secret")


def test_analysis_sends_video_and_prompt_and_saves_provenance(tmp_path):
    def respond(request):
        assert request.headers["x-goog-api-key"] == "test-key"
        assert request.url.path.endswith("/models/gemini-3.8-flash:generateContent")
        return httpx.Response(200, json={"candidates": [{
            "finishReason": "STOP", "content": {"parts": [{"text": "## 分析\n内容"}]}
        }]})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        preview = generate_video_preview(
            "https://youtu.be/vid", api_key="test-key", client=client, prompt="test"
        )
    markdown, record = write_video_preview(preview, tmp_path)
    assert markdown.read_text(encoding="utf-8") == "## 分析\n内容\n"
    assert json.loads(record.read_text(encoding="utf-8"))["prompt"] == "test"


def test_api_errors_do_not_leak_key():
    with httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(
        400, json={"error": {"message": "invalid key secret"}}
    ))) as client:
        with pytest.raises(GeminiPreviewError) as raised:
            generate_video_preview("https://youtu.be/vid", api_key="secret", client=client)
    assert "secret" not in str(raised.value)

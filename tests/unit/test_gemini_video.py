from __future__ import annotations

import json

import httpx
import pytest

from yt2class.gemini_video import (
    GeminiPreviewError,
    collect_video_urls,
    generate_video_preview,
    write_video_preview,
)


def test_collect_urls_combines_direct_and_file_inputs_without_duplicates(tmp_path):
    links = tmp_path / "links.txt"
    links.write_text(
        "# lessons\nhttps://youtu.be/second\nhttps://www.youtube.com/watch?v=first\n",
        encoding="utf-8",
    )

    assert collect_video_urls(["https://youtu.be/first"], links) == [
        "https://www.youtube.com/watch?v=first",
        "https://www.youtube.com/watch?v=second",
    ]


@pytest.mark.parametrize(
    "url",
    ["https://www.youtube.com/playlist?list=abc", "https://youtu.be/abc?list=playlist"],
)
def test_collect_urls_rejects_playlists(url):
    with pytest.raises(ValueError, match="playlist"):
        collect_video_urls([url], None)


def test_generate_preview_posts_video_uri_and_returns_model_text():
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {"finishReason": "STOP", "content": {"parts": [{"text": "## 核心摘要\n语法。"}]}}
                ],
                "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 12},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        preview = generate_video_preview(
            "https://youtu.be/first", api_key="test-key", client=client, prompt="Summarize"
        )

    assert preview["url"] == "https://www.youtube.com/watch?v=first"
    assert preview["text"] == "## 核心摘要\n语法。"
    assert preview["usage"]["promptTokenCount"] == 10
    assert preview["prompt"] == "Summarize"
    assert preview["model"] == "gemini-3.8-flash"
    assert requests[0].headers["x-goog-api-key"] == "test-key"
    assert requests[0].url.path.endswith("/models/gemini-3.8-flash:generateContent")
    body = json.loads(requests[0].content)
    assert body["contents"][0]["parts"] == [
        {"file_data": {"file_uri": "https://www.youtube.com/watch?v=first"}},
        {"text": "Summarize"},
    ]


def test_generate_preview_reports_rate_limit_without_retrying_or_leaking_key():
    count = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        return httpx.Response(429, json={"error": {"message": "quota exceeded"}})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(GeminiPreviewError, match="quota exceeded") as raised:
            generate_video_preview("https://youtu.be/first", api_key="private-key", client=client)
    assert count == 1
    assert "private-key" not in str(raised.value)


def test_generate_preview_redacts_key_if_server_echoes_it_in_error():
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                400, json={"error": {"message": "invalid key private-key"}}
            )
        )
    ) as client:
        with pytest.raises(GeminiPreviewError) as raised:
            generate_video_preview("https://youtu.be/first", api_key="private-key", client=client)
    assert "private-key" not in str(raised.value)


def test_generate_preview_rejects_empty_or_truncated_response():
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": []}}]},
            )
        )
    ) as client:
        with pytest.raises(GeminiPreviewError, match="MAX_TOKENS"):
            generate_video_preview("https://youtu.be/first", api_key="test-key", client=client)


def test_write_preview_saves_text_and_provenance_without_api_key(tmp_path):
    preview = {
        "url": "https://www.youtube.com/watch?v=first",
        "model": "gemini-3.8-flash",
        "prompt": "Summarize",
        "text": "## 核心摘要\n内容。",
        "usage": {},
        "generated_at": "2026-09-27T00:00:00Z",
    }

    markdown, record = write_video_preview(preview, tmp_path)

    assert markdown.name == "first.md"
    assert markdown.read_text(encoding="utf-8") == preview["text"] + "\n"
    assert json.loads(record.read_text(encoding="utf-8")) == preview

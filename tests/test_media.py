from pathlib import Path

from yt2class import media


def test_download_command_requests_video_and_preferred_subtitles(tmp_path: Path):
    assert hasattr(media, "build_download_command")

    command = media.build_download_command(
        "https://www.youtube.com/watch?v=FUn1gokaYoo", tmp_path / "media"
    )

    assert command[0] == "yt-dlp"
    assert "--write-auto-subs" in command
    assert "--sub-langs" in command
    assert "ja,zh-Hans,zh-Hant" in command
    assert "--skip-download" not in command


def test_frame_command_seeks_to_exact_timestamp(tmp_path: Path):
    assert hasattr(media, "build_frame_command")

    command = media.build_frame_command(
        tmp_path / "source.mp4", 12.5, tmp_path / "frame.jpg"
    )

    assert command[0] == "ffmpeg"
    assert "12.500" in command
    assert command[-1].endswith("frame.jpg")
    assert "-frames:v" in command


def test_title_command_is_single_video_metadata_lookup():
    assert hasattr(media, "build_title_command")
    command = media.build_title_command("https://www.youtube.com/watch?v=test")

    assert command[0] == "yt-dlp"
    assert "--no-playlist" in command
    assert "--skip-download" in command

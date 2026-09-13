from __future__ import annotations

from yt2class.workers.whisperx_worker import build_whisperx_command


def test_whisperx_worker_command_is_an_optional_process_boundary(tmp_path):
    command = build_whisperx_command(tmp_path / "audio.wav", model="tiny", device="cpu")
    assert command[:3] == [command[0], "-m", "yt2class.workers.whisperx_worker"]
    assert "--model" in command
    assert "tiny" in command

"""Probe external tool versions for manifest and cache keys."""

from __future__ import annotations

import shutil
import subprocess

from yt2class.domain.run_manifest import ToolVersions


def _run_version(command: list[str]) -> str | None:
    binary = command[0]
    if shutil.which(binary) is None:
        return None
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = (result.stdout or result.stderr or "").strip().splitlines()
    if not text:
        return None
    return text[0][:80]


def probe_tool_versions(
    *,
    producer_version: str = "yt2class-m5",
    provider: str | None = None,
    prompt: str | None = "m2-m4-bundle",
) -> ToolVersions:
    return ToolVersions(
        producer_version=producer_version,
        yt_dlp=_run_version(["yt-dlp", "--version"]),
        ffmpeg=_run_version(["ffmpeg", "-version"]),
        provider=provider,
        prompt=prompt,
    )


__all__ = ["probe_tool_versions"]

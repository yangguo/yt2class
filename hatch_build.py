"""Hatch build hook: install Node deps so the wheel bundles a runnable renderer."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class RendererBundleHook(BuildHookInterface):
    PLUGIN_NAME = "renderer-bundle"

    def initialize(self, version: str, build_data: dict) -> None:
        root = Path(self.root)
        renderer = root / "renderer"
        if not (renderer / "package.json").is_file():
            raise FileNotFoundError(
                "renderer/package.json is missing; cannot bundle yt2class/renderer_bundle"
            )
        npm = shutil.which("npm")
        if npm is None:
            raise RuntimeError("npm is required to build the yt2class wheel with PptxGenJS assets")
        subprocess.run([npm, "ci"], cwd=renderer, check=True)
        build_data.setdefault("force_include", {})
        build_data["force_include"]["renderer"] = "yt2class/renderer_bundle"

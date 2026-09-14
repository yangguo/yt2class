"""Renderer assets resolve from the installed package layout."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from yt2class.adapters.render.pptxgenjs import renderer_root


def test_renderer_package_json_and_entry_exist():
    root = renderer_root()
    assert (root / "package.json").is_file()
    assert (root / "src" / "render.mjs").is_file()


@pytest.mark.skipif(not shutil.which("node"), reason="node missing")
def test_renderer_node_modules_present_when_bundled():
    root = renderer_root()
    if not (root / "node_modules" / "pptxgenjs").is_dir():
        pytest.skip("run npm install in renderer and copy to src/yt2class/_renderer")
    assert (root / "node_modules" / "pptxgenjs" / "package.json").is_file()

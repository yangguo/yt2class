"""Renderer assets resolve from wheel installs without a source checkout."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(shutil.which("npm") is None, reason="npm required to build renderer bundle")
def test_wheel_bundles_renderer_and_resolves_from_clean_venv(tmp_path: Path):
    dist = REPO / "dist"
    if dist.exists():
        shutil.rmtree(dist)
    subprocess.run([sys.executable, "-m", "hatchling", "build"], cwd=REPO, check=True)
    wheels = list(dist.glob("*.whl"))
    assert wheels
    target = tmp_path / "site-packages"
    subprocess.run(
        [sys.executable, "-m", "pip", "install", str(wheels[0]), "--target", str(target)],
        check=True,
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(target)
    script = (
        "from yt2class.adapters.render.pptxgenjs import renderer_root; "
        "root = renderer_root(); "
        "assert (root / 'package.json').is_file(); "
        "assert (root / 'src' / 'render.mjs').is_file(); "
        "assert 'renderer_bundle' in str(root); "
        "print(root)"
    )
    out = subprocess.check_output([sys.executable, "-c", script], env=env, text=True).strip()
    bundled = Path(out)
    assert (bundled / "node_modules" / "pptxgenjs").is_dir()


def test_renderer_package_json_and_entry_exist():
    from yt2class.adapters.render.pptxgenjs import renderer_root

    root = renderer_root()
    assert (root / "package.json").is_file()
    assert (root / "src" / "render.mjs").is_file()

"""Renderer assets resolve from wheel installs without a source checkout."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _install_wheel_to_target(wheel: Path, target: Path) -> None:
    uv = shutil.which("uv")
    if uv:
        subprocess.run(
            [uv, "pip", "install", str(wheel), "--target", str(target)],
            check=True,
        )
        return
    try:
        import pip  # noqa: F401
    except ImportError:
        pytest.skip("neither uv nor pip available to install wheel into isolated target")
    subprocess.run(
        [sys.executable, "-m", "pip", "install", str(wheel), "--target", str(target)],
        check=True,
    )


@pytest.mark.skipif(shutil.which("npm") is None, reason="npm required to build renderer bundle")
def test_wheel_bundles_renderer_and_resolves_from_clean_venv(tmp_path: Path):
    dist = REPO / "dist"
    if dist.exists():
        shutil.rmtree(dist)
    subprocess.run([sys.executable, "-m", "hatchling", "build"], cwd=REPO, check=True)
    wheels = list(dist.glob("*.whl"))
    assert wheels
    target = tmp_path / "site-packages"
    _install_wheel_to_target(wheels[0], target)
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


def test_renderer_uses_fixed_image_size_release():
    from yt2class.adapters.render.pptxgenjs import renderer_root

    root = renderer_root()
    manifest = json.loads((root / "package.json").read_text(encoding="utf-8"))
    assert manifest["overrides"]["image-size"] == "2.0.4"
    lock = json.loads((root / "package-lock.json").read_text(encoding="utf-8"))
    assert lock["packages"]["node_modules/image-size"]["version"] == "2.0.4"

    package_json = root / "node_modules" / "image-size" / "package.json"
    if not package_json.is_file():
        pytest.skip("renderer node_modules missing")
    version = json.loads(package_json.read_text(encoding="utf-8"))["version"]
    assert version == "2.0.4"

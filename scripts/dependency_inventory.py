#!/usr/bin/env python3
"""Lightweight dependency inventory for release notes (not a formal SBOM)."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _python_freeze() -> str:
    uv = shutil.which("uv")
    if uv:
        return subprocess.check_output([uv, "pip", "freeze"], cwd=REPO, text=True)
    try:
        return subprocess.check_output([sys.executable, "-m", "pip", "freeze"], cwd=REPO, text=True)
    except subprocess.CalledProcessError:
        return "(pip freeze unavailable; run from `uv sync` environment)"


def main() -> int:
    print("# yt2class dependency inventory (informational)")
    print(f"repo: {REPO}")
    lock = REPO / "uv.lock"
    if lock.is_file():
        print(f"uv.lock: present ({lock.stat().st_size} bytes)")
    else:
        print("uv.lock: missing")
    print("\n## python (current env)\n")
    print(_python_freeze().strip())
    renderer_pkg = REPO / "renderer" / "package.json"
    if renderer_pkg.is_file():
        data = json.loads(renderer_pkg.read_text(encoding="utf-8"))
        deps = data.get("dependencies", {})
        print("\n## renderer/package.json dependencies\n")
        for name, version in sorted(deps.items()):
            print(f"{name}: {version}")
    print("\n# End — attach to release notes; not a compliance attestation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

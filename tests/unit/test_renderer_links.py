from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from yt2class.adapters.render.pptxgenjs import renderer_root


@pytest.mark.skipif(shutil.which("node") is None, reason="node required")
def test_primary_seek_seconds_falls_back_to_citation_evidence():
    root = renderer_root()
    script = """
import { primarySeekSeconds } from "./src/layouts/links.mjs";
const page = {
  type: "content",
  layout: "text",
  citation_ids: ["ev-cap-1", "ev-cap-2"],
};
const evidenceById = {
  "ev-cap-1": { id: "ev-cap-1", kind: "transcript", start_seconds: 42.5 },
  "ev-cap-2": { id: "ev-cap-2", kind: "transcript", start_seconds: 90.0 },
};
const seconds = primarySeekSeconds(page, {}, {}, evidenceById);
console.log(JSON.stringify({ seconds }));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout.strip())
    assert payload["seconds"] == 42.5

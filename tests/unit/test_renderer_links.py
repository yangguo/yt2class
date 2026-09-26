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


@pytest.mark.skipif(shutil.which("node") is None, reason="node required")
def test_page_seek_uses_its_own_evidence_and_unanchored_summary_has_no_footer():
    root = renderer_root()
    script = """
import { buildSeekLink, primarySeekSeconds } from "./src/layouts/links.mjs";
const pageAt257 = { type: "content", layout: "text", citation_ids: ["ev-cap-1"] };
const pageAt99 = { type: "content", layout: "text", citation_ids: ["ev-cap-2"] };
const summary = { type: "summary", citation_ids: ["ev-cap-1", "ev-cap-2"] };
const anchoredSummary = { type: "summary", citation_ids: ["ev-cap-1"] };
const noEvidence = { type: "content", layout: "text", citation_ids: ["ev-missing"] };
const evidenceById = {
  "ev-cap-1": { id: "ev-cap-1", kind: "transcript", start_seconds: 257.0 },
  "ev-cap-2": { id: "ev-cap-2", kind: "transcript", start_seconds: 99.0 },
};
const spec = { source: { kind: "local", media_path: "media/lesson.mp4" } };
const first = buildSeekLink(spec, pageAt257, {}, {}, evidenceById);
const second = buildSeekLink(spec, pageAt99, {}, {}, evidenceById);
const summaryLink = buildSeekLink(spec, summary, {}, {}, evidenceById);
const anchoredSummaryLink = buildSeekLink(spec, anchoredSummary, {}, {}, evidenceById);
const absent = buildSeekLink(spec, noEvidence, {}, {}, evidenceById);
console.log(JSON.stringify({
  first: first?.label ?? null,
  second: second?.label ?? null,
  summary: summaryLink?.label ?? null,
  anchoredSummary: anchoredSummaryLink?.label ?? null,
  absent: absent?.label ?? null,
  summarySeconds: primarySeekSeconds(summary, {}, {}, evidenceById),
  absentSeconds: primarySeekSeconds(noEvidence, {}, {}, evidenceById),
}));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout.strip())
    assert payload["first"] == "来源 04:17"
    assert payload["second"] == "来源 01:39"
    assert payload["summary"] is None
    assert payload["anchoredSummary"] == "来源 04:17"
    assert payload["absent"] is None
    assert payload["summarySeconds"] is None
    assert payload["absentSeconds"] is None


@pytest.mark.skipif(shutil.which("node") is None, reason="node required")
def test_content_bullets_prefer_learner_lines_over_claim_ids():
    root = renderer_root()
    script = """
import { contentBulletLines, formatLocalSeek } from "./src/layouts/links.mjs";
const lines = contentBulletLines(
  { bullets: ["1時間につき1500円", "一日につき300円"] },
  ["cap-0148 raw claim"],
);
const fallback = contentBulletLines({ bullets: [] }, ["雨天につき延期"]);
const label = formatLocalSeek("media/d344652e6eddd447.webm", 257);
console.log(JSON.stringify({ lines, fallback, label }));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout.strip())
    assert payload["lines"] == ["1時間につき1500円", "一日につき300円"]
    assert payload["fallback"] == ["雨天につき延期"]
    assert "webm" not in payload["label"].lower()
    assert "cap-" not in payload["label"]
    assert "04:17" in payload["label"]

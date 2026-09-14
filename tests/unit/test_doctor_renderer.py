from __future__ import annotations

import json
import os

from yt2class.orchestration.doctor import run_doctor


def test_invalid_renderer_root_reports_fail_without_traceback(monkeypatch):
    monkeypatch.setenv("YT2CLASS_RENDERER_ROOT", "/definitely/missing/renderer")
    report = run_doctor()
    payload = report.to_json()
    text = json.dumps(payload)
    assert "Traceback" not in text
    renderer = next(item for item in report.checks if item.name == "pptxgenjs_renderer")
    assert renderer.status == "fail"

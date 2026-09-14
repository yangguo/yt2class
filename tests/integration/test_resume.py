from __future__ import annotations

from pathlib import Path

import pytest

from yt2class.config import BuildSource, CourseConfig
from yt2class.orchestration import pipeline as orch_pipeline
from yt2class.orchestration.cache import CacheCorrupt, atomic_write_text, stage_artifact_path
from yt2class.orchestration.manifest_io import load_manifest, stage_record
from yt2class.orchestration.pipeline import execute_run
from tests.helpers.m5 import build_evidence_bundle

pytestmark = pytest.mark.skipif(
    not __import__("shutil").which("node"),
    reason="render stage requires node for PptxGenJS",
)


@pytest.mark.skipif(
    not (__import__("pathlib").Path(__import__("yt2class.adapters.render.pptxgenjs", fromlist=["renderer_root"]).renderer_root()) / "node_modules").is_dir(),
    reason="renderer node_modules missing",
)
def test_resume_skips_cached_analysis(tmp_path: Path, monkeypatch):
    workspace, bundle = build_evidence_bundle(tmp_path)
    calls = {"analysis": 0}
    real = orch_pipeline.analyze_evidence_bundle

    def counting(*args, **kwargs):
        calls["analysis"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(orch_pipeline, "analyze_evidence_bundle", counting)
    build = BuildSource(source_id=bundle.source_id)
    cfg = CourseConfig()

    first = execute_run(
        tmp_path,
        build=build,
        config=cfg,
        run_id=workspace.root.name,
        evidence_bundle=bundle,
        stop_after="reduce_knowledge",
    )
    assert calls["analysis"] == 1
    assert stage_record(first.manifest, "reduce_knowledge").status == "complete"

    second = execute_run(
        tmp_path,
        build=build,
        config=cfg,
        run_id=workspace.root.name,
        evidence_bundle=bundle,
        resume=True,
        stop_after="reduce_knowledge",
    )
    assert calls["analysis"] == 1
    assert stage_record(second.manifest, "reduce_knowledge").cache_key == stage_record(
        first.manifest, "reduce_knowledge"
    ).cache_key


def test_corrupt_cache_forces_rerun(tmp_path: Path, monkeypatch):
    workspace, bundle = build_evidence_bundle(tmp_path)
    build = BuildSource(source_id=bundle.source_id)
    cfg = CourseConfig()
    execute_run(
        tmp_path,
        build=build,
        config=cfg,
        run_id=workspace.root.name,
        evidence_bundle=bundle,
        stop_after="reduce_knowledge",
    )
    manifest = load_manifest(workspace.root)
    assert manifest is not None
    key = stage_record(manifest, "outline").cache_key
    assert key is not None
    corrupt = stage_artifact_path(workspace.root, "outline", key, "course-map.json")
    atomic_write_text(corrupt, "{not-json")

    calls = {"analysis": 0}
    real = orch_pipeline.analyze_evidence_bundle

    def counting(*args, **kwargs):
        calls["analysis"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(orch_pipeline, "analyze_evidence_bundle", counting)
    execute_run(
        tmp_path,
        build=build,
        config=cfg,
        run_id=workspace.root.name,
        evidence_bundle=bundle,
        resume=True,
        stop_after="reduce_knowledge",
    )
    assert calls["analysis"] == 1

"""Contract tests for the next pipeline's evidence-bound interchange format."""
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

import pytest
from pydantic import ValidationError


def payload():
    return {
        "schema_version": "2.0", "title": "示例课程", "language": "zh-CN",
        "source": {"kind": "local", "title": "示例", "media_path": "media/source.mp4", "sha256": "a" * 64, "duration_seconds": 60},
        "assets": [{"id": "f1", "path": "frames/f1.jpg", "sha256": sha256(b"image").hexdigest(), "timestamp_seconds": 12.5}],
        "evidence": [{"id": "e1", "kind": "frame", "start_seconds": 12, "end_seconds": 14, "asset_id": "f1"}],
        "slides": [{"id": "s1", "kind": "other", "title": "课程画面", "asset_id": "f1", "points": [{"text": "保留原始画面供复核。", "evidence_ids": ["e1"]}]}],
        "summary": [], "quiz": [], "analysis_mode": "offline",
    }


def test_valid_contract_and_assets(tmp_path):
    from yt2class.slide_spec import SlideSpec, validate_assets
    data = payload()
    data["source"]["sha256"] = sha256(b"video").hexdigest()
    spec = SlideSpec.model_validate(data)
    for name, content in [("frames/f1.jpg", b"image"), ("media/source.mp4", b"video")]:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    validate_assets(spec, tmp_path)
    (tmp_path / "frames/f1.jpg").write_bytes(b"changed")
    with pytest.raises(ValueError, match="hash"):
        validate_assets(spec, tmp_path)


@pytest.mark.parametrize("change", [
    lambda p: p.update(schema_version="9"),
    lambda p: p["slides"][0].update(image_url="https://invalid/image"),
    lambda p: p["slides"][0]["points"][0].update(evidence_ids=["missing"]),
    lambda p: p["slides"][0].update(asset_id="missing"),
    lambda p: p["assets"][0].update(path="../outside.jpg"),
    lambda p: p["assets"][0].update(path="/tmp/outside.jpg"),
    lambda p: p["assets"][0].update(timestamp_seconds=float("nan")),
    lambda p: p["assets"][0].update(timestamp_seconds=61),
    lambda p: p["evidence"][0].update(end_seconds=11),
    lambda p: p["evidence"][0].update(start_seconds=13),
    lambda p: p["slides"].append(deepcopy(p["slides"][0])),
    lambda p: p["evidence"].append(deepcopy(p["evidence"][0])),
    lambda p: p["slides"][0]["points"][0].update(evidence_ids=[]),
    lambda p: p["source"].update(kind="youtube"),
])
def test_contract_rejects_invalid_evidence(change):
    from yt2class.slide_spec import SlideSpec
    data = payload()
    change(data)
    with pytest.raises(ValidationError):
        SlideSpec.model_validate(data)


def test_symlink_escape_is_rejected(tmp_path):
    from yt2class.slide_spec import SlideSpec, validate_assets
    root = tmp_path / "run"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "media").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="outside"):
        validate_assets(SlideSpec.model_validate(payload()), root)


def test_published_schema_and_example_match_model():
    import json
    from yt2class.slide_spec import SlideSpec
    root = Path(__file__).resolve().parents[1]
    stored = json.loads((root / "docs/schemas/slide-spec.v2.schema.json").read_text())
    stored.pop("$schema")
    stored.pop("description")
    assert stored == SlideSpec.model_json_schema()
    SlideSpec.model_validate_json((root / "docs/examples/slide-spec.v2.json").read_text())


def test_transcript_evidence_and_chronological_slides():
    from yt2class.slide_spec import SlideSpec
    data = payload()
    data["evidence"].append({"id": "t1", "kind": "transcript", "start_seconds": 10,
                             "end_seconds": 13, "text": "例文", "origin": "manual-caption"})
    data["summary"] = [{"text": "例文", "evidence_ids": ["t1"]}]
    SlideSpec.model_validate(data)
    data["evidence"][-1]["origin"] = None
    with pytest.raises(ValidationError, match="origin"):
        SlideSpec.model_validate(data)
    data = payload()
    data["assets"].append({**data["assets"][0], "id": "f2", "timestamp_seconds": 5})
    data["evidence"].append({"id": "e2", "kind": "frame", "asset_id": "f2", "start_seconds": 5, "end_seconds": 6})
    data["slides"].append({**data["slides"][0], "id": "s2", "asset_id": "f2", "points": [{"text": "earlier", "evidence_ids": ["e2"]}]})
    with pytest.raises(ValidationError, match="time order"):
        SlideSpec.model_validate(data)

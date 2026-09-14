from __future__ import annotations

import json
from pathlib import Path

from yt2class.adapters.render.pptxgenjs import validate_pptx_package
from yt2class.domain.slide_spec_v3 import SlideSpecV3
from yt2class.orchestration.cache import mark_stage_complete
from yt2class.orchestration.cache_policy import validated_cache_hit

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/contracts/slide_spec_v3.valid.json"


def test_corrupt_pptx_cache_hit_rejected(tmp_path: Path):
    spec = SlideSpecV3.model_validate(json.loads(FIXTURE.read_text(encoding="utf-8")))
    pptx = tmp_path / "delivery" / "lesson.pptx"
    pptx.parent.mkdir(parents=True, exist_ok=True)
    pptx.write_bytes(b"truncated")
    render_key = "render-test-key"
    mark_stage_complete(tmp_path, "render", render_key)

    def _validate(_: Path) -> None:
        validate_pptx_package(pptx, spec=spec)

    assert not validated_cache_hit(tmp_path, "render", render_key, _validate)

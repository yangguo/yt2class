"""M4 binder/render fixtures with on-disk assets."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from PIL import Image

from yt2class.domain.source import SourceManifest
from yt2class.orchestration.workspace import Workspace
from yt2class.stages.edit_deck import edit_deck
from yt2class.stages.verify_claims import verify_claims
from tests.helpers.m3 import grounding_provider, lecture_knowledge


def _write_image(path: Path, color: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (320, 180), color)
    image.save(path, format="JPEG")
    return sha256(path.read_bytes()).hexdigest()


def seed_lecture_run(tmp_path: Path) -> tuple:
    doc, topics, transcript, visual = lecture_knowledge()
    provider = grounding_provider()
    plan = edit_deck(
        doc,
        course_map=topics,
        transcript=transcript,
        visual=visual,
        provider=provider,
        target_pages=10,
        max_pages=12,
    )
    outcome = verify_claims(
        doc,
        plan=plan,
        transcript=transcript,
        visual=visual,
        provider=provider,
        quality_mode="draft",
    )
    workspace = Workspace.create(tmp_path, run_id="run-m4")
    video = workspace.safe_path("media/source.mp4", create_parent=True)
    video_bytes = b"fixture-video-bytes"
    video.write_bytes(video_bytes)
    source_hash = sha256(video_bytes).hexdigest()
    updated_assets = []
    for asset in visual.assets:
        target = workspace.safe_path(asset.path, create_parent=True)
        digest = _write_image(target, "#ccddee")
        updated_assets.append(asset.model_copy(update={"sha256": digest}))
    visual = visual.model_copy(update={"assets": updated_assets})
    source = SourceManifest(
        schema_version="1.0",
        source_id=doc.source_id,
        kind="local",
        title="合成讲座",
        media_path="media/source.mp4",
        sha256=source_hash,
        duration_seconds=transcript.duration_seconds or 180.0,
        streams=[
            {
                "index": 0,
                "codec_type": "video",
                "codec_name": "h264",
            }
        ],
        timebase="1/90000",
        local_mode="copy",
    )
    return workspace, outcome, source, transcript, visual, topics


__all__ = ["seed_lecture_run"]

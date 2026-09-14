"""M5 orchestration fixtures: evidence bundle without ffmpeg."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from PIL import Image

from yt2class.domain.evidence import EvidenceBundle
from yt2class.domain.source import SourceManifest
from yt2class.orchestration.workspace import Workspace
from tests.helpers.m3 import lecture_knowledge


def build_evidence_bundle(tmp_path: Path) -> tuple[Workspace, EvidenceBundle]:
    doc, _topics, transcript, visual = lecture_knowledge()
    workspace = Workspace.create(tmp_path, run_id="run-m5")
    video = workspace.safe_path("media/source.mp4", create_parent=True)
    video_bytes = b"fixture-video-bytes-m5"
    video.write_bytes(video_bytes)
    source_hash = sha256(video_bytes).hexdigest()
    updated_assets = []
    for asset in visual.assets:
        target = workspace.safe_path(asset.path, create_parent=True)
        image = Image.new("RGB", (320, 180), "#aabbcc")
        image.save(target, format="JPEG")
        digest = sha256(target.read_bytes()).hexdigest()
        updated_assets.append(asset.model_copy(update={"sha256": digest}))
    visual = visual.model_copy(update={"assets": updated_assets})
    source = SourceManifest(
        schema_version="1.0",
        source_id=doc.source_id,
        kind="local",
        title="M5 fixture lecture",
        media_path="media/source.mp4",
        sha256=source_hash,
        duration_seconds=transcript.duration_seconds or 180.0,
        streams=[{"index": 0, "codec_type": "video", "codec_name": "h264"}],
        timebase="1/90000",
        local_mode="copy",
    )
    transcript_path = workspace.safe_path("evidence/transcript-document.json", create_parent=True)
    visual_path = workspace.safe_path("evidence/visual-catalogue.json", create_parent=True)
    transcript_path.write_text(transcript.model_dump_json(indent=2), encoding="utf-8")
    visual_path.write_text(visual.model_dump_json(indent=2), encoding="utf-8")
    transcript_rel = "evidence/transcript-document.json"
    visual_rel = "evidence/visual-catalogue.json"
    bundle = EvidenceBundle(
        schema_version="1.0",
        source_id=doc.source_id,
        source_hash=source_hash,
        duration_seconds=source.duration_seconds,
        source=source,
        transcript=transcript,
        visual=visual,
        status="complete",
        gaps=[],
        artifacts=[
            {
                "id": "artifact-transcript",
                "kind": "transcript-document",
                "path": transcript_rel,
                "sha256": sha256(transcript_path.read_bytes()).hexdigest(),
            },
            {
                "id": "artifact-visual",
                "kind": "visual-catalogue",
                "path": visual_rel,
                "sha256": sha256(visual_path.read_bytes()).hexdigest(),
            },
        ],
        transcript_path=transcript_rel,
        visual_path=visual_rel,
    )
    return workspace, bundle


__all__ = ["build_evidence_bundle"]

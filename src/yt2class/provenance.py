"""Build sources.json and page-level seek metadata from a bound SlideSpec."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import re
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from yt2class.domain.render_report import PageMapEntry
from yt2class.domain.slide_spec_v3 import (
    FrameEvidence,
    SlideClaim,
    SlidePage,
    SlideSpecV3,
    TranscriptEvidence,
)
from yt2class.orchestration.workspace import Workspace

YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass(frozen=True)
class ProvenanceResult:
    sources_path: str
    page_map: list[PageMapEntry]
    payload: dict[str, Any]


def _youtube_video_id(url: str) -> str | None:
    parsed = urlsplit(url.strip())
    host = (parsed.hostname or "").lower()
    if host == "youtu.be":
        video_id = parsed.path.strip("/").split("/", 1)[0]
    elif host.endswith("youtube.com") and parsed.path.rstrip("/").lower() == "/watch":
        values = parse_qs(parsed.query).get("v", [])
        video_id = values[0] if len(values) == 1 else ""
    else:
        return None
    return video_id if video_id and YOUTUBE_ID.fullmatch(video_id) else None


def canonical_youtube_seek(url: str | None, seconds: float) -> str:
    """Floor seek links for footers; precise seconds stay in notes."""

    if not url:
        return f"t={_format_seconds(seconds)}"
    video_id = _youtube_video_id(url)
    if video_id is None:
        return f"t={_format_seconds(seconds)}"
    base = f"https://www.youtube.com/watch?v={video_id}"
    parsed = urlsplit(base)
    query = urlencode({"v": video_id, "t": f"{max(0, int(math.floor(seconds)))}s"})
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))


def _format_seconds(seconds: float) -> str:
    if math.isclose(seconds, round(seconds)):
        return f"{int(round(seconds))}s"
    text = f"{seconds:.3f}".rstrip("0").rstrip(".")
    return f"{text}s"


def precise_time_note(seconds: float) -> str:
    return f"source_time={seconds:.3f}s"


def _claim_intervals(
    claim: SlideClaim,
    evidence_by_id: dict[str, Any],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for evidence_id in claim.evidence_ids:
        item = evidence_by_id.get(evidence_id)
        if isinstance(item, FrameEvidence):
            rows.append(
                {
                    "kind": "frame",
                    "evidence_id": evidence_id,
                    "seconds": item.timestamp_seconds,
                    "seek": item.timestamp_seconds,
                }
            )
        elif isinstance(item, TranscriptEvidence):
            rows.append(
                {
                    "kind": "transcript",
                    "evidence_id": evidence_id,
                    "start_seconds": item.start_seconds,
                    "end_seconds": item.end_seconds,
                    "text": item.text[:200],
                }
            )
    return rows


def _page_citations(
    page: SlidePage,
    *,
    claims: dict[str, SlideClaim],
    evidence_by_id: dict[str, Any],
    source_url: str | None,
    source_kind: Literal["youtube", "local"],
    media_name: str,
    source_hash: str,
) -> list[dict[str, Any]]:
    claim_ids: list[str] = []
    if page.type == "content" and page.layout == "sequence":
        for step in page.steps:
            claim_ids.extend(step.claim_ids)
    elif page.type == "summary":
        claim_ids = list(page.claim_ids)
    elif page.type == "quiz":
        for question in page.questions:
            claim_ids.extend(question.answer_claim_ids)
    else:
        claim_ids = list(page.point_claim_ids or page.claim_ids)

    citations: list[dict[str, Any]] = []
    if page.type == "content" and page.layout in {"comparison", "image-text", "sequence"}:
        frame_ids = list(page.frame_asset_ids)
        if page.layout == "sequence":
            frame_ids = [step.asset_id for step in page.steps]
        for index, asset_id in enumerate(frame_ids):
            stamp = 0.0
            for evidence_id in page.citation_ids:
                item = evidence_by_id.get(evidence_id)
                if isinstance(item, FrameEvidence) and item.asset_id == asset_id:
                    stamp = item.timestamp_seconds
                    break
            entry: dict[str, Any] = {
                "asset_id": asset_id,
                "index": index,
                "intervals": [],
            }
            if source_kind == "youtube":
                entry["seek_url"] = canonical_youtube_seek(source_url, stamp)
                entry["precise"] = precise_time_note(stamp)
            else:
                entry["media"] = media_name
                entry["sha256"] = source_hash
                entry["seek"] = precise_time_note(stamp)
            citations.append(entry)

    aggregate: list[dict[str, Any]] = []
    for claim_id in claim_ids:
        claim = claims.get(claim_id)
        if claim is None:
            continue
        intervals = _claim_intervals(claim, evidence_by_id)
        row = {"claim_id": claim_id, "intervals": intervals}
        if source_kind == "youtube":
            for interval in intervals:
                if interval.get("kind") == "frame":
                    interval["seek_url"] = canonical_youtube_seek(
                        source_url, float(interval["seconds"])
                    )
        aggregate.append(row)
    if page.type in {"summary", "quiz"}:
        citations = aggregate
    elif aggregate:
        citations.extend(aggregate)
    return citations


def build_provenance(
    spec: SlideSpecV3,
    *,
    workspace: Workspace,
    page_map: list[PageMapEntry] | None = None,
    relative_path: str = "delivery/sources.json",
) -> ProvenanceResult:
    claims = {claim.id: claim for claim in spec.claims}
    evidence_by_id = {item.id: item for item in spec.evidence}
    media_name = Path(spec.source.media_path).name
    pages_payload: list[dict[str, Any]] = []
    built_map = page_map or [
        PageMapEntry(
            page_id=slide.id,
            pptx_slide_index=index,
            layout=slide.layout or slide.type,
        )
        for index, slide in enumerate(spec.slides)
    ]
    for slide, mapped in zip(spec.slides, built_map, strict=True):
        pages_payload.append(
            {
                "page_id": slide.id,
                "pptx_slide_index": mapped.pptx_slide_index,
                "layout": mapped.layout,
                "title": slide.title,
                "citations": _page_citations(
                    slide,
                    claims=claims,
                    evidence_by_id=evidence_by_id,
                    source_url=spec.source.url,
                    source_kind=spec.source.kind,
                    media_name=media_name,
                    source_hash=spec.source.sha256,
                ),
                "notes": slide.notes,
            }
        )
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "source": {
            "source_id": spec.source.source_id,
            "kind": spec.source.kind,
            "title": spec.source.title,
            "media_path": spec.source.media_path,
            "sha256": spec.source.sha256,
            "duration_seconds": spec.source.duration_seconds,
            "url": spec.source.url,
        },
        "pages": pages_payload,
    }
    out = workspace.safe_path(relative_path, create_parent=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return ProvenanceResult(sources_path=relative_path, page_map=built_map, payload=payload)


__all__ = [
    "ProvenanceResult",
    "build_provenance",
    "canonical_youtube_seek",
    "precise_time_note",
]

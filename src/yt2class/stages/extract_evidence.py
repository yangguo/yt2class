"""M1 evidence extraction stage: captions/ASR plus scenes/frames/OCR."""

from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
from threading import Event
from typing import Callable

from yt2class.adapters.asr import ASRCancelled, ASRError, ASRRequest, ASRResult, run_asr
from yt2class.adapters.ocr import OCRCancelled, OCRStatus, ocr_density, run_ocr
from yt2class.adapters.scenes import SceneCancelled, SceneError, SceneProfile, extract_visual_catalogue
from yt2class.adapters.subtitles import (
    SubtitleError,
    build_transcript_document,
    choose_subtitle_track,
)
from yt2class.domain.evidence import EvidenceArtifact, EvidenceBundle, EvidenceGap
from yt2class.domain.source import SourceManifest
from yt2class.domain.transcript import SpeechCoverage, TranscriptDocument, TranscriptGap, TranscriptSegment
from yt2class.domain.visual import OcrRegion, VisualCatalogue, VisualGap
from yt2class.orchestration.workspace import WorkspacePathError


Runner = Callable[..., object]


class EvidenceCancelled(RuntimeError):
    """Raised when a caller cancellation interrupts any evidence modality."""


def _empty_transcript(source_id: str, duration: float, reason: str) -> TranscriptDocument:
    gap = TranscriptGap(id="gap-0001", start_seconds=0.0, end_seconds=duration, reason=reason)
    return TranscriptDocument(
        schema_version="1.0",
        source_id=source_id,
        language="und",
        raw_artifact_hash=None,
        alignment="none",
        speech_coverage=SpeechCoverage(
            speech_seconds=0.0,
            covered_seconds=0.0,
            denominator="timeline",
            denominator_seconds=duration,
            coverage_ratio=0.0,
        ),
        segments=[],
        duration_seconds=duration,
        status="degraded",
        gaps=[gap],
    )


def _transcript_from_asr(result: ASRResult, *, source_id: str, duration: float) -> TranscriptDocument:
    if result.segments and result.raw_artifact_hash is None:
        raise ASRError("ASR result with segments is missing its result hash")
    segments: list[TranscriptSegment] = []
    for segment in result.segments:
        segment_end = min(duration, segment.end_seconds)
        if segment.start_seconds >= duration or not segment.start_seconds < segment_end:
            continue
        quality_flags = list(segment.quality_flags)
        words = []
        for word in segment.words or []:
            start = max(segment.start_seconds, word.start_seconds)
            end = min(segment_end, word.end_seconds)
            if not start < end:
                if "word-outside-clipped-segment" not in quality_flags:
                    quality_flags.append("word-outside-clipped-segment")
                continue
            if start != word.start_seconds or end != word.end_seconds:
                if "word-outside-clipped-segment" not in quality_flags:
                    quality_flags.append("word-outside-clipped-segment")
            words.append(word.model_copy(update={"start_seconds": start, "end_seconds": end}))
        segments.append(
            TranscriptSegment(
                id=segment.id,
                start_seconds=segment.start_seconds,
                end_seconds=segment_end,
                text_original=segment.text_original,
                language=segment.language,
                origin="asr",
                words=words or None,
                speaker_id=segment.speaker_id,
                alignment_status=segment.alignment_status,
                quality_flags=quality_flags,
            )
        )
    intervals = sorted((segment.start_seconds, segment.end_seconds) for segment in segments)
    merged: list[list[float]] = []
    for start, end in intervals:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    covered = sum(end - start for start, end in merged)
    gaps: list[TranscriptGap] = []
    cursor = 0.0
    for start, end in merged:
        if cursor < start:
            gaps.append(
                TranscriptGap(
                    id=f"gap-{len(gaps)+1:04d}",
                    start_seconds=cursor,
                    end_seconds=start,
                    reason="uncovered-asr-range",
                )
            )
        cursor = max(cursor, end)
    if cursor < duration:
        gaps.append(
            TranscriptGap(
                id=f"gap-{len(gaps)+1:04d}",
                start_seconds=cursor,
                end_seconds=duration,
                reason="uncovered-asr-range",
            )
        )
    if result.error:
        gaps.append(
            TranscriptGap(
                id=f"gap-{len(gaps)+1:04d}",
                start_seconds=0.0,
                end_seconds=duration,
                reason=result.error,
            )
        )
    ratio = covered / duration if duration else 0.0
    return TranscriptDocument(
        schema_version="1.0",
        source_id=source_id,
        language=result.language,
        raw_artifact_hash=result.raw_artifact_hash,
        alignment=result.alignment,
        speech_coverage=SpeechCoverage(
            speech_seconds=covered,
            covered_seconds=covered,
            denominator="timeline",
            denominator_seconds=duration,
            coverage_ratio=ratio,
        ),
        segments=segments,
        duration_seconds=duration,
        status="complete" if result.status == "complete" and segments and not gaps else "degraded",
        gaps=gaps,
    )


def _write_json_atomic(path: Path, payload: object) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
    return encoded


def _empty_visual(source_id: str, duration: float, reason: str) -> VisualCatalogue:
    return VisualCatalogue(
        schema_version="1.0",
        source_id=source_id,
        scenes=[],
        assets=[],
        occurrences=[],
        ocr_regions=[],
        profile=None,
        status="failed",
        gaps=[VisualGap(id="visual-gap-0001", start_seconds=0.0, end_seconds=duration, reason=reason)],
    )


def _safe_output_path(run_root: Path, relative: str, *, workspace: object | None) -> Path:
    """Resolve an evidence output and reject symlink/traversal escapes before writing."""

    if workspace is not None:
        return workspace.safe_path(relative, create_parent=True)  # type: ignore[attr-defined]
    root = Path(run_root).expanduser().resolve(strict=True)
    candidate = root / relative
    resolved = candidate.resolve(strict=False)
    if not resolved.is_relative_to(root):
        raise WorkspacePathError(f"evidence output resolves outside workspace: {relative}")
    candidate.parent.mkdir(parents=True, exist_ok=True)
    resolved = candidate.resolve(strict=False)
    if not resolved.is_relative_to(root):
        raise WorkspacePathError(f"evidence output resolves outside workspace: {relative}")
    return candidate


def _relative_output_path(path: Path, run_root: Path) -> str:
    root = Path(run_root).expanduser().resolve(strict=True)
    resolved = path.resolve(strict=False)
    if not resolved.is_relative_to(root):
        raise WorkspacePathError(f"evidence artifact escapes run root: {path}")
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError as error:
        raise WorkspacePathError(f"evidence artifact escapes run root: {path}") from error


def _extract_evidence_unlocked(
    source: SourceManifest | None = None,
    media_path: Path | None = None,
    run_root: Path | None = None,
    *,
    source_manifest: SourceManifest | None = None,
    output_dir: Path | None = None,
    workspace: object | None = None,
    sidecar: object = None,
    manual: object = None,
    auto: object = None,
    profile: SceneProfile = "content",
    detector: Callable[..., list[tuple[float, float]]] | None = None,
    runner: Runner = subprocess.run,
    ocr_engine: str = "none",
    ocr_runner: Runner = subprocess.run,
    asr_request: ASRRequest | None = None,
    asr_runner: Runner = subprocess.run,
    cancel_event: Event | None = None,
) -> EvidenceBundle:
    """Build a deterministic evidence bundle without a page-count cutoff."""

    if cancel_event is not None and cancel_event.is_set():
        raise EvidenceCancelled("evidence extraction cancelled before start")
    source = source or source_manifest
    if source is None:
        raise ValueError("extract_evidence requires a SourceManifest")
    if workspace is not None:
        workspace_root = Path(getattr(workspace, "root")).expanduser().resolve(strict=True)
        supplied_root = run_root or output_dir
        if supplied_root is not None and Path(supplied_root).expanduser().resolve(strict=True) != workspace_root:
            raise WorkspacePathError("run_root/output_dir does not match workspace root")
        run_root = workspace_root
    elif run_root is None:
        run_root = output_dir
    if run_root is None:
        raise ValueError("extract_evidence requires run_root/output_dir/workspace")
    if media_path is None:
        if source.reference_path:
            media_path = Path(source.reference_path)
        elif workspace is not None:
            media_path = workspace.safe_path(source.media_path)  # type: ignore[attr-defined]
        else:
            media_path = Path(run_root) / source.media_path
    run_root = Path(run_root)
    if workspace is not None:
        # Validate both output trees before any external process can publish a
        # frame or evidence document through a symlink.
        workspace.safe_path("frames", create_parent=True)  # type: ignore[attr-defined]
        workspace.safe_path("evidence", create_parent=True)  # type: ignore[attr-defined]
    duration = source.duration_seconds
    selected = choose_subtitle_track(sidecar=sidecar, manual=manual, auto=auto)
    transcript: TranscriptDocument
    if selected is not None:
        try:
            transcript = build_transcript_document(
                selected,
                source_id=source.source_id,
                duration_seconds=duration,
            )
        except SubtitleError as error:
            transcript = _empty_transcript(source.source_id, duration, f"subtitle failure: {error}")
    elif asr_request is not None:
        try:
            asr_result = run_asr(asr_request, runner=asr_runner, cancel_event=cancel_event)
            if asr_result.status in {"failed", "cancelled"}:
                raise ASRError(asr_result.error or f"ASR status is {asr_result.status}")
            transcript = _transcript_from_asr(
                asr_result,
                source_id=source.source_id,
                duration=duration,
            )
        except ASRCancelled as error:
            raise EvidenceCancelled(f"ASR cancellation: {error}") from error
        except ASRError as error:
            transcript = _empty_transcript(source.source_id, duration, f"ASR failure: {error}")
    else:
        transcript = _empty_transcript(source.source_id, duration, "no subtitle or ASR evidence")

    try:
        visual = extract_visual_catalogue(
            source_id=source.source_id,
            video_path=Path(media_path),
            run_root=run_root,
            duration_seconds=duration,
            profile=profile,
            detector=detector,
            runner=runner,
            cancel_event=cancel_event,
        )
    except SceneCancelled as error:
        raise EvidenceCancelled(f"frame extraction cancellation: {error}") from error
    except SceneError as error:
        visual = _empty_visual(source.source_id, duration, f"visual failure: {error}")

    bundle_gaps: list[EvidenceGap] = []
    for gap in transcript.gaps:
        bundle_gaps.append(
            EvidenceGap(
                id=f"transcript-{gap.id}",
                modality="transcript",
                start_seconds=gap.start_seconds,
                end_seconds=gap.end_seconds,
                reason=gap.reason,
                status="degraded",
            )
        )
    for gap in visual.gaps:
        bundle_gaps.append(
            EvidenceGap(
                id=f"visual-{gap.id}",
                modality="visual",
                start_seconds=gap.start_seconds,
                end_seconds=gap.end_seconds,
                reason=gap.reason,
                status="degraded" if visual.status != "failed" else "failed",
            )
        )

    asset_by_id = {asset.id: asset for asset in visual.assets}
    ocr_regions: list[OcrRegion] = []
    occurrence_updates = {}
    visual_degraded_by_ocr = False
    if ocr_engine in {"none", "", "unavailable"}:
        visual_degraded_by_ocr = True
        bundle_gaps.append(
            EvidenceGap(
                id="ocr-unavailable",
                modality="ocr",
                start_seconds=0.0,
                end_seconds=duration,
                reason="OCR engine is not configured",
                status="unavailable",
            )
        )
    else:
        for occurrence in visual.occurrences:
            asset = asset_by_id[occurrence.asset_id]
            image_path = run_root / asset.path
            try:
                result = run_ocr(
                    image_path,
                    asset_id=asset.id,
                    occurrence_id=occurrence.id,
                    engine=ocr_engine,
                    runner=ocr_runner,
                    cancel_event=cancel_event,
                )
            except OCRCancelled as error:
                raise EvidenceCancelled(f"OCR cancellation: {error}") from error
            if result.status == OCRStatus.COMPLETE:
                ocr_regions.extend(
                    OcrRegion(
                        id=region.id,
                        asset_id=region.asset_id,
                        parent_occurrence_id=region.parent_occurrence_id,
                        bbox=region.bbox,
                        text=region.text,
                        engine=region.engine,
                        confidence=region.confidence,
                    )
                    for region in result.regions
                )
                asset_dimensions = (asset.width or 1, asset.height or 1)
                text_for_density = " ".join(region.text for region in result.regions)
                occurrence_updates[occurrence.id] = occurrence.model_copy(
                    update={
                        "quality": occurrence.quality.model_copy(
                            update={
                                "ocr_density": ocr_density(
                                    text_for_density,
                                    width=asset_dimensions[0],
                                    height=asset_dimensions[1],
                                )
                            }
                        )
                    }
                )
            else:
                visual_degraded_by_ocr = True
                timestamp = (
                    occurrence.actual_source_seconds
                    if occurrence.actual_source_seconds is not None
                    else occurrence.timestamp_seconds
                )
                if timestamp is None:
                    timestamp = occurrence.requested_seconds
                bundle_gaps.append(
                    EvidenceGap(
                        id=f"ocr-{occurrence.id}",
                        modality="ocr",
                        start_seconds=timestamp,
                        end_seconds=min(duration, timestamp + 0.001),
                        reason=result.error or "OCR unavailable",
                        status="unavailable" if result.status == OCRStatus.UNAVAILABLE else "failed",
                    )
                )
    if occurrence_updates:
        visual = visual.model_copy(
            update={
                "ocr_regions": ocr_regions,
                "occurrences": [
                    occurrence_updates.get(occurrence.id, occurrence)
                    for occurrence in visual.occurrences
                ],
            }
        )
    else:
        visual = visual.model_copy(update={"ocr_regions": ocr_regions})
    if visual_degraded_by_ocr and visual.status == "complete":
        visual = visual.model_copy(update={"status": "degraded"})

    if cancel_event is not None and cancel_event.is_set():
        raise EvidenceCancelled("evidence extraction cancelled")

    evidence_dir = _safe_output_path(run_root, "evidence", workspace=workspace)
    transcript_path = _safe_output_path(
        run_root,
        "evidence/transcript-document.json",
        workspace=workspace,
    )
    visual_path = _safe_output_path(
        run_root,
        "evidence/visual-catalogue.json",
        workspace=workspace,
    )
    transcript_bytes = _write_json_atomic(transcript_path, transcript.model_dump(mode="json"))
    visual_bytes = _write_json_atomic(visual_path, visual.model_dump(mode="json"))
    artifacts = [
        EvidenceArtifact(
            id="artifact-transcript",
            kind="transcript-document",
            path=_relative_output_path(transcript_path, run_root),
            sha256=sha256(transcript_bytes).hexdigest(),
        ),
        EvidenceArtifact(
            id="artifact-visual",
            kind="visual-catalogue",
            path=_relative_output_path(visual_path, run_root),
            sha256=sha256(visual_bytes).hexdigest(),
        ),
    ]
    bundle = EvidenceBundle(
        schema_version="1.0",
        source_id=source.source_id,
        source_hash=source.sha256,
        duration_seconds=duration,
        source=source,
        transcript=transcript,
        visual=visual,
        status=(
            "complete"
            if not bundle_gaps
            and transcript.status == "complete"
            and visual.status == "complete"
            else "degraded"
        ),
        gaps=bundle_gaps,
        artifacts=artifacts,
        transcript_path=artifacts[0].path,
        visual_path=artifacts[1].path,
    )
    _write_json_atomic(
        _safe_output_path(run_root, "evidence/evidence-bundle.json", workspace=workspace),
        bundle.model_dump(mode="json"),
    )
    return bundle


def extract_evidence(
    source: SourceManifest | None = None,
    media_path: Path | None = None,
    run_root: Path | None = None,
    *,
    source_manifest: SourceManifest | None = None,
    output_dir: Path | None = None,
    workspace: object | None = None,
    lock: bool = True,
    sidecar: object = None,
    manual: object = None,
    auto: object = None,
    profile: SceneProfile = "content",
    detector: Callable[..., list[tuple[float, float]]] | None = None,
    runner: Runner = subprocess.run,
    ocr_engine: str = "none",
    ocr_runner: Runner = subprocess.run,
    asr_request: ASRRequest | None = None,
    asr_runner: Runner = subprocess.run,
    cancel_event: Event | None = None,
) -> EvidenceBundle:
    """Extract evidence, taking the workspace single-writer lock when requested."""

    if workspace is not None and lock:
        with workspace.write_lock():  # type: ignore[attr-defined]
            return _extract_evidence_unlocked(
                source,
                media_path,
                run_root,
                source_manifest=source_manifest,
                output_dir=output_dir,
                workspace=workspace,
                sidecar=sidecar,
                manual=manual,
                auto=auto,
                profile=profile,
                detector=detector,
                runner=runner,
                ocr_engine=ocr_engine,
                ocr_runner=ocr_runner,
                asr_request=asr_request,
                asr_runner=asr_runner,
                cancel_event=cancel_event,
            )
    return _extract_evidence_unlocked(
        source,
        media_path,
        run_root,
        source_manifest=source_manifest,
        output_dir=output_dir,
        workspace=workspace,
        sidecar=sidecar,
        manual=manual,
        auto=auto,
        profile=profile,
        detector=detector,
        runner=runner,
        ocr_engine=ocr_engine,
        ocr_runner=ocr_runner,
        asr_request=asr_request,
        asr_runner=asr_runner,
        cancel_event=cancel_event,
    )


def extract_evidence_locked(
    *args: object,
    **kwargs: object,
) -> EvidenceBundle:
    """Explicit alias for callers already holding ``Workspace.write_lock``."""

    kwargs["lock"] = False
    return extract_evidence(*args, **kwargs)  # type: ignore[arg-type]


__all__ = ["EvidenceCancelled", "extract_evidence", "extract_evidence_locked"]

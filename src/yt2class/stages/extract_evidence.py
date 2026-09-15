"""M1 evidence extraction stage: captions/ASR plus scenes/frames/OCR."""

from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
from threading import Event
from typing import Callable

from yt2class.adapters.ytdlp import downloaded_subtitle
from yt2class.adapters.asr import (
    ASRCancelled,
    ASRError,
    ASRRequest,
    ASRResult,
    extract_audio_with_provenance,
    run_asr,
)
from pydantic import ValidationError

from yt2class.adapters.ocr import OCRCancelled, OCRContractError, OCRStatus, ocr_density, run_ocr
from yt2class.adapters.scenes import SceneCancelled, SceneError, SceneProfile, extract_visual_catalogue
from yt2class.adapters.subtitles import (
    SubtitleError,
    build_transcript_document,
    choose_subtitle_track,
)
from yt2class.domain.evidence import EvidenceArtifact, EvidenceBundle, EvidenceGap
from yt2class.domain.source import SourceInputError, SourceManifest, content_sha256
from yt2class.domain.transcript import SpeechCoverage, TranscriptDocument, TranscriptGap, TranscriptSegment
from yt2class.stages.transcript_visual_correction import correct_transcript_from_visual
from yt2class.domain.visual import FrameOccurrence, OcrRegion, VisualCatalogue, VisualGap
from yt2class.orchestration.workspace import WorkspacePathError
from yt2class.stages.ingest import IngestError, resolve_manifest_media


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
    if result.source_id != source_id:
        raise ASRError(
            f"ASR result source_id {result.source_id!r} does not match SourceManifest {source_id!r}"
        )
    if result.audio_sha256 is None or result.parent_hash is None:
        raise ASRError("ASR result is missing audio input hash provenance")
    if result.command_digest is None:
        raise ASRError("ASR result is missing audio transform command digest")
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
        audio_input_hash=result.audio_sha256,
        audio_parent_hash=result.parent_hash,
        audio_command_digest=result.command_digest,
        time_offset_seconds=result.offset_seconds,
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


def _prepare_run_root(
    run_root: Path | None,
    *,
    output_dir: Path | None,
    workspace: object | None,
) -> Path:
    """Create and confine the run root before any evidence write or fallback."""

    if workspace is not None:
        workspace_root = Path(getattr(workspace, "root")).expanduser().resolve(strict=True)
        supplied = run_root or output_dir
        if supplied is not None:
            candidate = Path(supplied).expanduser()
            resolved = candidate.resolve(strict=candidate.exists())
            if resolved != workspace_root:
                raise WorkspacePathError("run_root/output_dir does not match workspace root")
        workspace.safe_path("frames", create_parent=True)  # type: ignore[attr-defined]
        workspace.safe_path("evidence", create_parent=True)  # type: ignore[attr-defined]
        return workspace_root
    root = run_root or output_dir
    if root is None:
        raise ValueError("extract_evidence requires run_root/output_dir/workspace")
    root = Path(root).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    resolved = root.resolve(strict=True)
    (resolved / "frames").mkdir(parents=True, exist_ok=True)
    (resolved / "evidence").mkdir(parents=True, exist_ok=True)
    return resolved


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


def _snapshot_verified_media(
    source: Path,
    *,
    expected_hash: str,
    dest: Path,
) -> Path:
    """Copy verified media into immutable run storage before extraction."""

    source = Path(source).expanduser()
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        source_resolved = source.resolve(strict=True)
    except OSError as error:
        raise ValueError(f"verified media does not exist: {source}") from error
    if dest.exists():
        try:
            dest_resolved = dest.resolve(strict=True)
        except OSError as error:
            raise ValueError(f"cannot resolve verified media snapshot: {dest}") from error
        if source_resolved == dest_resolved:
            try:
                actual = content_sha256(dest)
            except (OSError, SourceInputError) as error:
                raise ValueError(f"cannot hash verified media snapshot: {dest}") from error
            if actual != expected_hash:
                raise ValueError(f"verified media hash does not match: {dest}")
            return dest
    temporary = dest.with_name(f".{dest.name}.part")
    temporary.unlink(missing_ok=True)
    try:
        shutil.copy2(source, temporary)
        actual = content_sha256(temporary)
    except (OSError, SourceInputError) as error:
        temporary.unlink(missing_ok=True)
        raise ValueError(f"cannot snapshot verified media: {source}") from error
    if actual != expected_hash:
        temporary.unlink(missing_ok=True)
        raise ValueError("verified media changed before snapshot")
    try:
        temporary.replace(dest)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise ValueError(f"cannot snapshot verified media: {source}") from error
    return dest


def _discard_derived_outputs(run_root: Path) -> None:
    wav = Path(run_root) / "tmp" / "asr-audio.wav"
    wav.unlink(missing_ok=True)
    frames = Path(run_root) / "frames"
    if frames.is_dir():
        for child in frames.iterdir():
            if child.is_file() or child.is_symlink():
                child.unlink(missing_ok=True)


def _assert_verified_media_unchanged(path: Path, expected_hash: str) -> None:
    try:
        actual = content_sha256(Path(path))
    except (OSError, SourceInputError) as error:
        raise ValueError(f"cannot rehash verified media after extraction: {path}") from error
    if actual != expected_hash:
        raise ValueError("verified media changed during extraction")


def _relative_output_path(path: Path, run_root: Path) -> str:
    root = Path(run_root).expanduser().resolve(strict=True)
    resolved = path.resolve(strict=False)
    if not resolved.is_relative_to(root):
        raise WorkspacePathError(f"evidence artifact escapes run root: {path}")
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError as error:
        raise WorkspacePathError(f"evidence artifact escapes run root: {path}") from error


def _rebuild_visual_with_ocr(
    visual: VisualCatalogue,
    ocr_regions: list[OcrRegion],
    occurrence_updates: dict[str, FrameOccurrence],
) -> VisualCatalogue:
    payload = visual.model_dump(mode="json")
    payload["ocr_regions"] = [region.model_dump(mode="json") for region in ocr_regions]
    if occurrence_updates:
        payload["occurrences"] = [
            occurrence_updates.get(occurrence.id, occurrence).model_dump(mode="json")
            for occurrence in visual.occurrences
        ]
    return VisualCatalogue.model_validate(payload)


def _ocr_gap(
    occurrence: FrameOccurrence,
    duration: float,
    reason: str,
    *,
    status: str = "failed",
) -> EvidenceGap:
    timestamp = (
        occurrence.actual_source_seconds
        if occurrence.actual_source_seconds is not None
        else occurrence.timestamp_seconds
    )
    if timestamp is None:
        timestamp = occurrence.requested_seconds
    return EvidenceGap(
        id=f"ocr-{occurrence.id}",
        modality="ocr",
        start_seconds=timestamp,
        end_seconds=min(duration, timestamp + 0.001),
        reason=reason,
        status=status,  # type: ignore[arg-type]
    )


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
    ocr_languages: str = "jpn+eng",
    ocr_unavailable_reason: str | None = None,
    ocr_runner: Runner = subprocess.run,
    asr_request: ASRRequest | None = None,
    asr_runner: Runner = subprocess.run,
    audio_runner: Runner = subprocess.run,
    cancel_event: Event | None = None,
) -> EvidenceBundle:
    """Build a deterministic evidence bundle without a page-count cutoff."""

    if cancel_event is not None and cancel_event.is_set():
        raise EvidenceCancelled("evidence extraction cancelled before start")
    source = source or source_manifest
    if source is None:
        raise ValueError("extract_evidence requires a SourceManifest")
    run_root = _prepare_run_root(run_root, output_dir=output_dir, workspace=workspace)
    try:
        media_path = resolve_manifest_media(
            source,
            workspace,  # type: ignore[arg-type]
            media_path=media_path,
            run_root=run_root,
        )
    except IngestError as error:
        raise ValueError(str(error)) from error
    if source.kind == "youtube" and sidecar is None and manual is None and auto is None:
        track = downloaded_subtitle(Path(media_path))
        if track is not None:
            if track.origin == "manual-caption":
                manual = [track]
            else:
                auto = [track]
    suffix = Path(media_path).suffix.lower() or ".mp4"
    media_path = _snapshot_verified_media(
        Path(media_path),
        expected_hash=source.sha256,
        dest=_safe_output_path(
            run_root,
            f"media/{source.sha256[:16]}{suffix}",
            workspace=workspace,
        ),
    )
    duration = source.duration_seconds
    selected = choose_subtitle_track(sidecar=sidecar, manual=manual, auto=auto)
    subtitle_transcript: TranscriptDocument | None = None
    if selected is not None:
        try:
            subtitle_transcript = build_transcript_document(
                selected,
                source_id=source.source_id,
                duration_seconds=duration,
            )
        except SubtitleError as error:
            subtitle_transcript = _empty_transcript(
                source.source_id, duration, f"subtitle failure: {error}"
            )
    transcript: TranscriptDocument
    if subtitle_transcript is not None and subtitle_transcript.segments:
        transcript = subtitle_transcript
    elif asr_request is not None:
        try:
            if asr_request.source_id != source.source_id:
                raise ASRError(
                    f"ASR request source_id {asr_request.source_id!r} does not match "
                    f"SourceManifest {source.source_id!r}"
                )
            extracted = extract_audio_with_provenance(
                Path(media_path),
                _safe_output_path(run_root, "tmp/asr-audio.wav", workspace=workspace),
                runner=audio_runner,
                cancel_event=cancel_event,
            )
            if extracted.parent_hash != source.sha256:
                raise ASRError("extracted audio parent hash does not match SourceManifest")
            caller_audio = Path(asr_request.audio_path).expanduser()
            if caller_audio.exists():
                try:
                    caller_resolved = caller_audio.resolve(strict=True)
                except OSError as error:
                    raise ASRError(f"cannot resolve ASR audio_path: {caller_audio}") from error
                if caller_resolved != Path(media_path).resolve():
                    try:
                        caller_hash = content_sha256(caller_resolved)
                    except OSError as error:
                        raise ASRError(f"cannot hash ASR audio_path: {caller_resolved}") from error
                    if caller_hash not in {extracted.audio_sha256, source.sha256}:
                        raise ASRError("ASR audio is not derived from verified source media")
            bound_request = asr_request.model_copy(update={"audio_path": extracted.output_path})
            asr_result = run_asr(bound_request, runner=asr_runner, cancel_event=cancel_event)
            if asr_result.status in {"failed", "cancelled"}:
                raise ASRError(asr_result.error or f"ASR status is {asr_result.status}")
            if asr_result.audio_sha256 != extracted.audio_sha256:
                raise ASRError("ASR audio hash does not match extracted source audio")
            if asr_result.parent_hash not in {None, extracted.parent_hash}:
                raise ASRError("ASR result parent hash does not match extracted audio provenance")
            asr_result = asr_result.model_copy(
                update={
                    "parent_hash": extracted.parent_hash,
                    "command_digest": extracted.command_digest,
                }
            )
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
        transcript = subtitle_transcript or _empty_transcript(
            source.source_id, duration, "no subtitle or ASR evidence"
        )

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
    try:
        _assert_verified_media_unchanged(media_path, source.sha256)
    except ValueError as error:
        _discard_derived_outputs(run_root)
        reason = str(error)
        transcript = _empty_transcript(source.source_id, duration, reason)
        visual = _empty_visual(source.source_id, duration, reason)

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
                reason=ocr_unavailable_reason or "OCR engine is not configured",
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
                    languages=ocr_languages if ocr_engine == "tesseract" else None,
                    runner=ocr_runner,
                    cancel_event=cancel_event,
                )
            except OCRCancelled as error:
                raise EvidenceCancelled(f"OCR cancellation: {error}") from error
            except OCRContractError as error:
                visual_degraded_by_ocr = True
                bundle_gaps.append(_ocr_gap(occurrence, duration, str(error)))
                continue
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
                bundle_gaps.append(
                    _ocr_gap(
                        occurrence,
                        duration,
                        result.error or "OCR unavailable",
                        status="unavailable" if result.status == OCRStatus.UNAVAILABLE else "failed",
                    )
                )
    accepted_regions: list[OcrRegion] = []
    accepted_updates: dict[str, FrameOccurrence] = {}
    try:
        visual = _rebuild_visual_with_ocr(visual, ocr_regions, occurrence_updates)
        accepted_regions = ocr_regions
        accepted_updates = occurrence_updates
    except ValidationError:
        visual_degraded_by_ocr = True
        by_occurrence: dict[str, list[OcrRegion]] = {}
        for region in ocr_regions:
            by_occurrence.setdefault(region.parent_occurrence_id, []).append(region)
        for occurrence in visual.occurrences:
            candidate_regions = accepted_regions + by_occurrence.get(occurrence.id, [])
            candidate_updates = dict(accepted_updates)
            if occurrence.id in occurrence_updates:
                candidate_updates[occurrence.id] = occurrence_updates[occurrence.id]
            if candidate_regions == accepted_regions and candidate_updates == accepted_updates:
                continue
            try:
                _rebuild_visual_with_ocr(visual, candidate_regions, candidate_updates)
            except ValidationError as error:
                bundle_gaps.append(
                    _ocr_gap(
                        occurrence,
                        duration,
                        f"OCR catalogue validation failed: {error}",
                    )
                )
                continue
            accepted_regions = candidate_regions
            accepted_updates = candidate_updates
        visual = _rebuild_visual_with_ocr(visual, accepted_regions, accepted_updates)
    if visual_degraded_by_ocr and visual.status == "complete":
        visual = visual.model_copy(update={"status": "degraded"})

    transcript, _visual_asr_corrections = correct_transcript_from_visual(transcript, visual)

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
    ocr_languages: str = "jpn+eng",
    ocr_unavailable_reason: str | None = None,
    ocr_runner: Runner = subprocess.run,
    asr_request: ASRRequest | None = None,
    asr_runner: Runner = subprocess.run,
    audio_runner: Runner = subprocess.run,
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
                ocr_languages=ocr_languages,
                ocr_unavailable_reason=ocr_unavailable_reason,
                ocr_runner=ocr_runner,
                asr_request=asr_request,
                asr_runner=asr_runner,
                audio_runner=audio_runner,
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
        ocr_languages=ocr_languages,
        ocr_unavailable_reason=ocr_unavailable_reason,
        ocr_runner=ocr_runner,
        asr_request=asr_request,
        asr_runner=asr_runner,
        audio_runner=audio_runner,
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

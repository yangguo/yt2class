"""Bounded evidence refinement: at most two extra rounds per segment."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import Callable, Literal

from yt2class.adapters.providers.base import Provider, ProviderCapabilities
from yt2class.domain.course_map import CourseMap
from yt2class.domain.knowledge import KnowledgeEvidenceRequest, KnowledgeUnit, Uncertainty
from yt2class.domain.segment import AnalysisWindow
from yt2class.domain.source import SourceManifest
from yt2class.domain.transcript import TranscriptDocument
from yt2class.domain.visual import FrameOccurrence, VisualAsset, VisualCatalogue
from yt2class.orchestration.scheduler import dispatch_fits, rebuild_window_batches
from yt2class.stages.analyze_segments import SegmentAnalysisOutcome, analyze_window

RefinementKind = Literal["unreadable_text", "missing_step", "audio_visual_conflict"]
RejectReason = Literal[
    "accepted",
    "out_of_range",
    "modality_not_allowed",
    "budget_exhausted",
    "round_limit",
    "clip_duration",
    "extraction_failed",
]

MAX_ROUNDS = 2
MAX_EXTRA_FRAMES = 4
MIN_CLIP_SECONDS = 5.0
MAX_CLIP_SECONDS = 30.0


@dataclass
class RefinementBudget:
    max_rounds: int = MAX_ROUNDS
    max_extra_frames_per_round: int = MAX_EXTRA_FRAMES
    remaining_frames: int = MAX_ROUNDS * MAX_EXTRA_FRAMES
    remaining_clip_seconds: float = MAX_CLIP_SECONDS
    rounds_used: dict[str, int] = field(default_factory=dict)

    def rounds_for(self, segment_id: str) -> int:
        return self.rounds_used.get(segment_id, 0)

    def consume_round(self, segment_id: str) -> None:
        self.rounds_used[segment_id] = self.rounds_for(segment_id) + 1


@dataclass(frozen=True)
class RefinementDecision:
    accepted: bool
    reason: RejectReason
    request: KnowledgeEvidenceRequest
    note: str


@dataclass(frozen=True)
class ExtractedFrame:
    occurrence: FrameOccurrence
    asset: VisualAsset


@dataclass(frozen=True)
class ExtractedClip:
    asset: VisualAsset
    start_seconds: float
    end_seconds: float

    def as_payload(self) -> dict[str, float | str]:
        return {
            "id": self.asset.id,
            "start_seconds": self.start_seconds,
            "end_seconds": self.end_seconds,
            "sha256": self.asset.sha256,
            "path": self.asset.path,
        }


@dataclass
class RefinementPull:
    frames: list[FrameOccurrence] = field(default_factory=list)
    assets: list[VisualAsset] = field(default_factory=list)
    clips: list[ExtractedClip] = field(default_factory=list)
    clip_seconds: float = 0.0
    unresolved: bool = False
    note: str = ""


FrameExtractor = Callable[[float, Path], ExtractedFrame | FrameOccurrence | None]
ClipExtractor = Callable[[float, float, Path], ExtractedClip | None]


def classify_request(request: KnowledgeEvidenceRequest) -> RefinementKind:
    text = request.reason.lower()
    if "unreadable" in text or "ocr" in text or "看不清" in request.reason:
        return "unreadable_text"
    if "missing_step" in text or "步骤" in request.reason or "step" in text:
        return "missing_step"
    return "audio_visual_conflict"


def accept_refinement_request(
    request: KnowledgeEvidenceRequest,
    *,
    window: AnalysisWindow,
    duration_seconds: float,
    capabilities: ProviderCapabilities,
    budget: RefinementBudget,
    segment_id: str,
) -> RefinementDecision:
    if budget.rounds_for(segment_id) >= budget.max_rounds:
        return RefinementDecision(False, "round_limit", request, "max two rounds per segment")
    if request.start_seconds < 0 or request.end_seconds > duration_seconds + 1e-9:
        return RefinementDecision(False, "out_of_range", request, "request exceeds source duration")
    if request.end_seconds <= request.start_seconds:
        return RefinementDecision(False, "out_of_range", request, "empty request range")
    overlap_start = max(request.start_seconds, window.context_start_seconds)
    overlap_end = min(request.end_seconds, window.context_end_seconds)
    if overlap_end <= overlap_start:
        return RefinementDecision(False, "out_of_range", request, "request is outside the analysis window")
    if overlap_start != request.start_seconds or overlap_end != request.end_seconds:
        request = request.model_copy(
            update={"start_seconds": overlap_start, "end_seconds": overlap_end}
        )

    kind = classify_request(request)
    if request.desired_modality == "clip" or (
        kind == "missing_step" and request.desired_modality == "clip"
    ):
        if not capabilities.supports_video:
            return RefinementDecision(False, "modality_not_allowed", request, "provider cannot take clips")
        length = request.end_seconds - request.start_seconds
        if length < MIN_CLIP_SECONDS or length > MAX_CLIP_SECONDS:
            return RefinementDecision(
                False, "clip_duration", request, "clips must be 5-30 seconds"
            )
        if budget.remaining_clip_seconds < length:
            return RefinementDecision(False, "budget_exhausted", request, "clip budget exhausted")
    if request.desired_modality in {"frame", "audio"} or kind == "unreadable_text":
        if request.desired_modality == "frame" and not capabilities.supports_images:
            return RefinementDecision(False, "modality_not_allowed", request, "provider cannot take frames")
        if request.desired_modality == "audio" and not capabilities.supports_audio:
            return RefinementDecision(False, "modality_not_allowed", request, "provider cannot take audio")
        if request.desired_modality == "frame" and budget.remaining_frames <= 0:
            return RefinementDecision(False, "budget_exhausted", request, "frame budget exhausted")
    return RefinementDecision(True, "accepted", request, "in range, modality allowed, in budget")


def nearby_sample_times(start: float, end: float, *, existing: list[float], limit: int) -> list[float]:
    mid = (start + end) / 2.0
    left = start + (end - start) * 0.25
    right = start + (end - start) * 0.75
    candidates = [mid, left, right, start, max(start, end - 1e-3)]
    times: list[float] = []
    for stamp in candidates + existing:
        if not start <= stamp < end:
            continue
        if any(abs(stamp - seen) < 1e-3 for seen in times):
            continue
        times.append(stamp)
        if len(times) >= limit:
            break
    return times


def _as_extracted_frame(extracted: ExtractedFrame | FrameOccurrence | None) -> ExtractedFrame | None:
    if extracted is None:
        return None
    if isinstance(extracted, ExtractedFrame):
        return extracted
    return None


def pull_refinement_evidence(
    request: KnowledgeEvidenceRequest,
    *,
    visual: VisualCatalogue,
    duration_seconds: float,
    budget: RefinementBudget,
    segment_id: str,
    output_dir: Path | None = None,
    frame_extractor: FrameExtractor | None = None,
    clip_extractor: ClipExtractor | None = None,
) -> RefinementPull:
    del duration_seconds
    pulled = RefinementPull()
    dest = output_dir or Path(".")
    if request.desired_modality == "clip":
        if clip_extractor is None:
            pulled.unresolved = True
            pulled.note = "clip extractor required; refusing to invent media"
            budget.consume_round(segment_id)
            return pulled
        extracted = clip_extractor(request.start_seconds, request.end_seconds, dest / "clips")
        if extracted is None:
            pulled.unresolved = True
            pulled.note = "clip extraction failed"
            budget.consume_round(segment_id)
            return pulled
        pulled.clips.append(extracted)
        pulled.assets.append(extracted.asset)
        pulled.clip_seconds = extracted.end_seconds - extracted.start_seconds
        budget.remaining_clip_seconds = max(0.0, budget.remaining_clip_seconds - pulled.clip_seconds)
        budget.consume_round(segment_id)
        return pulled

    if frame_extractor is None:
        pulled.unresolved = True
        pulled.note = "frame extractor required; refusing to invent frames"
        budget.consume_round(segment_id)
        return pulled

    existing = [
        float(occurrence.timestamp_seconds or occurrence.requested_seconds)
        for occurrence in visual.occurrences
        if (occurrence.timestamp_seconds or occurrence.requested_seconds) is not None
    ]
    times = nearby_sample_times(
        request.start_seconds,
        request.end_seconds,
        existing=existing,
        limit=min(budget.max_extra_frames_per_round, budget.remaining_frames),
    )
    if not times:
        pulled.unresolved = True
        pulled.note = "no nearby sample times"
        budget.consume_round(segment_id)
        return pulled

    for stamp in times:
        extracted = _as_extracted_frame(frame_extractor(stamp, dest / "frames"))
        if extracted is None:
            continue
        pulled.frames.append(extracted.occurrence)
        pulled.assets.append(extracted.asset)
    if not pulled.frames:
        pulled.unresolved = True
        pulled.note = "frame extraction failed"
        budget.consume_round(segment_id)
        return pulled
    budget.remaining_frames = max(0, budget.remaining_frames - len(pulled.frames))
    budget.consume_round(segment_id)
    return pulled


def merge_visual_catalogue(
    visual: VisualCatalogue,
    pulled: RefinementPull,
) -> VisualCatalogue:
    if not pulled.frames and not pulled.assets:
        return visual
    assets = list(visual.assets)
    seen_assets = {asset.id for asset in assets}
    for asset in pulled.assets:
        if asset.id not in seen_assets:
            assets.append(asset)
            seen_assets.add(asset.id)
    occurrences = list(visual.occurrences)
    seen = {item.id for item in occurrences}
    for occurrence in pulled.frames:
        if occurrence.id not in seen:
            occurrences.append(occurrence)
            seen.add(occurrence.id)
    return visual.model_copy(update={"assets": assets, "occurrences": occurrences})


def _requests_from_units(units: list[KnowledgeUnit]) -> list[KnowledgeEvidenceRequest]:
    found: list[KnowledgeEvidenceRequest] = []
    for unit in units:
        found.extend(unit.evidence_requests)
        for uncertainty in unit.uncertainty:
            if uncertainty.kind in {"unreadable_text", "missing_step", "conflict"}:
                found.append(
                    KnowledgeEvidenceRequest(
                        start_seconds=uncertainty.start_seconds,
                        end_seconds=uncertainty.end_seconds,
                        reason=uncertainty.kind,
                        desired_modality="clip" if uncertainty.kind == "missing_step" else "frame",
                    )
                )
    return found


def mark_unresolved(units: list[KnowledgeUnit], note: str) -> list[KnowledgeUnit]:
    updated: list[KnowledgeUnit] = []
    for unit in units:
        claims = [
            claim.model_copy(update={"status": "unresolved"})
            if claim.status == "draft"
            else claim
            for claim in unit.claims
        ]
        uncertainty = list(unit.uncertainty)
        if unit.start_seconds < unit.end_seconds:
            uncertainty.append(
                Uncertainty(
                    kind="missing_step" if "step" in note else "unreadable_text",
                    start_seconds=unit.start_seconds,
                    end_seconds=unit.end_seconds,
                    note=note[:400],
                )
            )
        updated.append(
            unit.model_copy(update={"claims": claims, "uncertainty": uncertainty, "evidence_requests": []})
        )
    return updated


def refine_window(
    outcome: SegmentAnalysisOutcome,
    *,
    transcript: TranscriptDocument,
    visual: VisualCatalogue,
    course_map: CourseMap,
    provider: Provider,
    capabilities: ProviderCapabilities,
    duration_seconds: float,
    source: SourceManifest | None = None,
    media_path: Path | None = None,
    output_dir: Path | None = None,
    frame_extractor: FrameExtractor | None = None,
    clip_extractor: ClipExtractor | None = None,
    budget: RefinementBudget | None = None,
    cancel_event: Event | None = None,
) -> tuple[SegmentAnalysisOutcome, VisualCatalogue, RefinementBudget]:
    """Pull nearby HD frames or 5-30s clips, then re-analyze only this window."""

    del source, media_path
    budget = budget or RefinementBudget()
    current_visual = visual
    current = outcome
    requests = _requests_from_units(current.units)
    if not requests:
        return current, current_visual, budget

    extra_clips: list[ExtractedClip] = []
    while requests:
        segment_id = current.window.id
        accepted_any = False
        unresolved_note = ""
        for request in requests:
            decision = accept_refinement_request(
                request,
                window=current.window,
                duration_seconds=duration_seconds,
                capabilities=capabilities,
                budget=budget,
                segment_id=segment_id,
            )
            if not decision.accepted:
                unresolved_note = decision.note
                continue
            pulled = pull_refinement_evidence(
                decision.request,
                visual=current_visual,
                duration_seconds=duration_seconds,
                budget=budget,
                segment_id=segment_id,
                output_dir=output_dir,
                frame_extractor=frame_extractor,
                clip_extractor=clip_extractor,
            )
            if pulled.unresolved:
                unresolved_note = pulled.note
                continue
            current_visual = merge_visual_catalogue(current_visual, pulled)
            extra_clips.extend(pulled.clips)
            accepted_any = True
        if not accepted_any:
            current = SegmentAnalysisOutcome(
                window=current.window.model_copy(
                    update={"status": "degraded", "failure_reason": unresolved_note or "unresolved"}
                ),
                payload=current.payload,
                units=mark_unresolved(current.units, unresolved_note or "refinement rejected"),
                repaired=current.repaired,
                error=unresolved_note,
            )
            break
        refreshed = rebuild_window_batches(
            current.window.model_copy(update={"status": "running"}),
            transcript=transcript,
            visual=current_visual,
            capabilities=capabilities,
            course_map=course_map,
            extra_clips=extra_clips,
        )
        if not dispatch_fits(refreshed, capabilities, extra_clips=extra_clips):
            note = "unschedulable: refinement request exceeds provider budget"
            current = SegmentAnalysisOutcome(
                window=current.window.model_copy(
                    update={"status": "degraded", "failure_reason": note}
                ),
                payload=current.payload,
                units=mark_unresolved(current.units, note),
                repaired=current.repaired,
                error=note,
            )
            break
        current = analyze_window(
            refreshed,
            transcript=transcript,
            visual=current_visual,
            course_map=course_map,
            provider=provider,
            cancel_event=cancel_event,
            request_suffix=f":round-{budget.rounds_for(segment_id)}",
            extra_clips=extra_clips,
        )
        requests = _requests_from_units(current.units)
    return current, current_visual, budget

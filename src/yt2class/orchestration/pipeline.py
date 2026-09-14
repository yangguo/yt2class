"""M5 stage DAG: manifest, cache, resume, and delivery wiring."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import Any

from yt2class.adapters.ytdlp import downloaded_subtitle
from yt2class.adapters.providers.base import Provider
from yt2class.adapters.providers.factory import (
    UnsupportedAnalysisProvider,
    resolve_course_provider,
)
from yt2class.adapters.providers.openrouter import OpenRouterProvider
from yt2class.config import BuildSource, CourseConfig, write_desensitized_snapshot
from yt2class.domain.evidence import EvidenceBundle
from yt2class.domain.run_manifest import RunManifest, StageName
from yt2class.domain.source import SourceInput, SourceManifest
from yt2class.domain.transcript import TranscriptDocument
from yt2class.domain.visual import VisualCatalogue
from yt2class.orchestration.analyze import (
    AnalysisResult,
    analyze_evidence_bundle,
    resolve_native_adapter,
    write_analysis_artifacts,
)
from yt2class.orchestration.budget import BudgetExceeded, RunBudget
from yt2class.orchestration.cache import (
    CacheCorrupt,
    atomic_write_json,
    compute_cache_key,
    digest_parts,
    invalidate_from,
    load_validated_json,
    mark_stage_complete,
    stage_artifact_path,
    stage_cache_hit,
)
from yt2class.orchestration.cache_policy import (
    invalidate_stage_tree,
    model_digest,
    refresh_stage_keys,
    validated_cache_hit,
)
from yt2class.orchestration.provider_gate import wrap_provider
from yt2class.orchestration.retry import RetryPolicy
from yt2class.orchestration.run_request import (
    load_run_request,
    merge_build_for_resume,
    record_from_build,
    save_run_request,
    subtitles_digest,
)
from yt2class.orchestration.tool_probe import probe_tool_versions
from yt2class.orchestration.delivery import bind_plan_to_spec, render_spec
from yt2class.orchestration.edit import plan_deck, verify_plan, write_editorial_artifacts
from yt2class.orchestration.manifest_io import (
    ALL_STAGES,
    initial_manifest,
    load_manifest,
    new_run_id,
    reset_stages,
    save_manifest,
    set_stage_status,
    stage_record,
)
from yt2class.orchestration.workspace import Workspace
from yt2class.stages.extract_evidence import extract_evidence_locked
from yt2class.stages.ingest import ingest_source_locked


class PipelineError(RuntimeError):
    """Orchestration failure for one run."""


class PipelinePaused(PipelineError):
    """Budget exhausted or human review required before continuing."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass
class RunOutcome:
    manifest: RunManifest
    workspace: Workspace
    pptx_path: Path | None = None
    review_html: Path | None = None


@dataclass
class RunContext:
    workspace: Workspace
    config: CourseConfig
    budget: RunBudget
    manifest: RunManifest
    cancel_event: Event | None = None
    provider: Provider | None = None
    force_stages: set[StageName] = field(default_factory=set)
    stop_after: StageName | None = None
    build: BuildSource | None = None
    source: SourceManifest | None = None
    evidence: EvidenceBundle | None = None
    analysis: AnalysisResult | None = None

    def tool_versions(self) -> dict[str, str | None]:
        tv = self.manifest.tool_versions
        provider_label = tv.provider or (
            f"{self.config.analysis.provider}:{self.config.analysis.model_profile}"
        )
        return {
            "producer_version": tv.producer_version,
            "yt_dlp": tv.yt_dlp,
            "ffmpeg": tv.ffmpeg,
            "provider": provider_label,
            "prompt": tv.prompt or "m2-m4-bundle",
        }

    def review_revision(self) -> int:
        record = load_run_request(self.workspace.root)
        return record.review_revision if record is not None else 0

    def config_digest(self) -> str:
        return self.config.config_digest()


def _provider(ctx: RunContext) -> Provider:
    if ctx.provider is not None:
        inner = ctx.provider
    else:
        visual = ctx.evidence.visual if ctx.evidence is not None else None
        if ctx.analysis is not None:
            visual = ctx.analysis.visual
        try:
            inner = resolve_course_provider(
                ctx.config.analysis.provider,
                ctx.config.analysis,
                run_root=ctx.workspace.root,
                visual=visual,
            )
        except UnsupportedAnalysisProvider as error:
            raise PipelineError(str(error)) from error
    if isinstance(inner, OpenRouterProvider):
        visual = ctx.evidence.visual if ctx.evidence is not None else None
        if ctx.analysis is not None:
            visual = ctx.analysis.visual
        inner.bind_run_context(ctx.workspace.root, visual=visual)
    retry_policy = None if ctx.config.analysis.provider == "fake" else RetryPolicy()
    return wrap_provider(
        inner,
        ctx.budget,
        cancel_event=ctx.cancel_event,
        retry_policy=retry_policy,
    )


def _invalidate_from(ctx: RunContext, stage: StageName) -> None:
    invalidate_stage_tree(ctx.workspace.root, stage)
    names = (stage,) + tuple(invalidate_from(stage))
    ctx.manifest = reset_stages(ctx.manifest, names)
    _persist(ctx)


def _persist(ctx: RunContext) -> None:
    save_manifest(ctx.workspace.root, ctx.manifest)


def _reconcile_resume_cache(ctx: RunContext) -> None:
    stale: list[StageName] = []
    for name in ALL_STAGES:
        record = stage_record(ctx.manifest, name)
        if record.status != "complete" or not record.cache_key:
            continue
        if not stage_cache_hit(ctx.workspace.root, name, record.cache_key):
            stale.append(name)
    if stale:
        for name in stale:
            invalidate_stage_tree(ctx.workspace.root, name)
        ctx.manifest = reset_stages(ctx.manifest, tuple(stale))
        _persist(ctx)


def _should_run(ctx: RunContext, stage: StageName, cache_key: str) -> bool:
    if stage in ctx.force_stages:
        return True
    return not stage_cache_hit(ctx.workspace.root, stage, cache_key)


def _complete_stage(ctx: RunContext, stage: StageName, cache_key: str) -> None:
    mark_stage_complete(ctx.workspace.root, stage, cache_key)
    ctx.manifest = set_stage_status(ctx.manifest, stage, "complete", cache_key=cache_key)
    _persist(ctx)


def _fail_stage(ctx: RunContext, stage: StageName, message: str) -> None:
    ctx.manifest = set_stage_status(ctx.manifest, stage, "failed", error=message[:400])
    ctx.manifest = ctx.manifest.model_copy(update={"errors": [*ctx.manifest.errors, message[:400]]})
    _persist(ctx)
    raise PipelineError(message)


def _ingest_input_hashes(build: BuildSource) -> list[str]:
    parts: list[str] = []
    if build.video is not None and build.video.is_file():
        from yt2class.orchestration.cache import file_sha256

        parts.append(file_sha256(build.video))
    elif build.url:
        parts.append(digest_parts(build.url))
    sub = subtitles_digest(build.subtitles)
    parts.append(sub or "no-subtitles")
    return parts


def run_ingest(ctx: RunContext, build: BuildSource) -> SourceManifest:
    stage: StageName = "ingest"
    cache_key = compute_cache_key(
        stage,
        config_digest=ctx.config_digest(),
        input_hashes=_ingest_input_hashes(build),
        tool_versions=ctx.tool_versions(),
    )
    ctx.manifest = refresh_stage_keys(
        ctx.manifest,
        ctx.workspace.root,
        stage,
        cache_key,
        save=lambda m: save_manifest(ctx.workspace.root, m),
    )
    artifact = stage_artifact_path(ctx.workspace.root, stage, cache_key, "source-manifest.json")

    def _validate(_cache_dir: Path) -> None:
        load_validated_json(artifact, SourceManifest)

    if validated_cache_hit(ctx.workspace.root, stage, cache_key, _validate):
        manifest = load_validated_json(artifact, SourceManifest)
        ctx.source = manifest
        ctx.manifest = set_stage_status(ctx.manifest, stage, "complete", cache_key=cache_key)
        _persist(ctx)
        return manifest

    ctx.manifest = set_stage_status(ctx.manifest, stage, "running")
    _persist(ctx)
    if build.video is not None:
        source_input = SourceInput.from_value(build.video)
    elif build.url:
        source_input = SourceInput.from_value(build.url)
    else:
        _fail_stage(ctx, stage, "build source requires --url or --video")
    try:
        with ctx.workspace.write_lock():
            result = ingest_source_locked(
                source_input,
                ctx.workspace,
                source_id=build.source_id,
            )
    except Exception as error:  # noqa: BLE001
        _fail_stage(ctx, stage, f"ingest failed: {error}")
    atomic_write_json(artifact, result.manifest.model_dump(mode="json"))
    _complete_stage(ctx, stage, cache_key)
    ctx.source = result.manifest
    return result.manifest


def _extract_input_hashes(ctx: RunContext, source: SourceManifest) -> list[str]:
    sub = "no-subtitles"
    if ctx.build and ctx.build.subtitles:
        sub = subtitles_digest(ctx.build.subtitles) or "no-subtitles"
    else:
        record = load_run_request(ctx.workspace.root)
        if record and record.subtitles_sha256:
            sub = record.subtitles_sha256
    if sub == "no-subtitles" and source.kind == "youtube":
        track = downloaded_subtitle(ctx.workspace.safe_path(source.media_path))
        if track is not None:
            sub = digest_parts(subtitles_digest(track.path) or "", track.language, track.origin)
    return [source.sha256, sub]


def run_extract_evidence(ctx: RunContext, source: SourceManifest) -> EvidenceBundle:
    stage: StageName = "extract_evidence"
    cache_key = compute_cache_key(
        stage,
        config_digest=ctx.config_digest(),
        input_hashes=_extract_input_hashes(ctx, source),
        tool_versions=ctx.tool_versions(),
    )
    ctx.manifest = refresh_stage_keys(
        ctx.manifest,
        ctx.workspace.root,
        stage,
        cache_key,
        save=lambda m: save_manifest(ctx.workspace.root, m),
    )
    artifact = stage_artifact_path(ctx.workspace.root, stage, cache_key, "evidence-bundle.json")

    def _validate(_cache_dir: Path) -> None:
        load_validated_json(artifact, EvidenceBundle)

    if validated_cache_hit(ctx.workspace.root, stage, cache_key, _validate):
        bundle = load_validated_json(artifact, EvidenceBundle)
        ctx.evidence = bundle
        atomic_write_json(
            ctx.workspace.safe_path("evidence/evidence-bundle.json", create_parent=True),
            bundle.model_dump(mode="json"),
        )
        _complete_stage(ctx, stage, cache_key)
        return bundle

    ctx.manifest = set_stage_status(ctx.manifest, stage, "running")
    _persist(ctx)
    try:
        with ctx.workspace.write_lock():
            sidecar = ctx.build.subtitles if ctx.build and ctx.build.subtitles else None
            bundle = extract_evidence_locked(
                source,
                workspace=ctx.workspace,
                sidecar=sidecar,
                cancel_event=ctx.cancel_event,
            )
    except Exception as error:  # noqa: BLE001
        _fail_stage(ctx, stage, f"extract_evidence failed: {error}")
    atomic_write_json(artifact, bundle.model_dump(mode="json"))
    atomic_write_json(
        ctx.workspace.safe_path("evidence/evidence-bundle.json", create_parent=True),
        bundle.model_dump(mode="json"),
    )
    _complete_stage(ctx, stage, cache_key)
    ctx.evidence = bundle
    return bundle


def _write_analysis_cache(ctx: RunContext, stage: StageName, cache_key: str, filename: str, payload: Any) -> None:
    path = stage_artifact_path(ctx.workspace.root, stage, cache_key, filename)
    atomic_write_json(path, payload.model_dump(mode="json"))


def run_analysis_stages(ctx: RunContext, bundle: EvidenceBundle) -> AnalysisResult:
    provider = _provider(ctx)
    base_inputs = [
        bundle.source_hash,
        digest_parts(bundle.transcript.raw_artifact_hash or ""),
        model_digest(bundle.transcript),
        model_digest(bundle.visual),
        ctx.config.analysis.mode,
    ]
    outline_key = compute_cache_key(
        "outline",
        config_digest=ctx.config_digest(),
        input_hashes=base_inputs,
        tool_versions=ctx.tool_versions(),
    )
    segments_key = compute_cache_key(
        "analyze_segments",
        config_digest=ctx.config_digest(),
        input_hashes=[*base_inputs, outline_key],
        tool_versions=ctx.tool_versions(),
    )
    reduce_key = compute_cache_key(
        "reduce_knowledge",
        config_digest=ctx.config_digest(),
        input_hashes=[*base_inputs, segments_key],
        tool_versions=ctx.tool_versions(),
    )

    if (
        not _should_run(ctx, "reduce_knowledge", reduce_key)
        and stage_cache_hit(ctx.workspace.root, "reduce_knowledge", reduce_key)
    ):
        knowledge_path = stage_artifact_path(
            ctx.workspace.root, "reduce_knowledge", reduce_key, "knowledge-document.json"
        )
        course_path = stage_artifact_path(ctx.workspace.root, "outline", outline_key, "course-map.json")
        segments_path = stage_artifact_path(
            ctx.workspace.root, "analyze_segments", segments_key, "segment-manifest.json"
        )
        from yt2class.domain.course_map import CourseMap
        from yt2class.domain.knowledge import KnowledgeDocument
        from yt2class.domain.segment import SegmentManifest

        try:
            result = AnalysisResult(
                course_map=load_validated_json(course_path, CourseMap),
                segments=load_validated_json(segments_path, SegmentManifest),
                knowledge=load_validated_json(knowledge_path, KnowledgeDocument),
                visual=bundle.visual,
                outcomes=[],
                coverage_complete=True,
                gap_reasons=[],
            )
        except CacheCorrupt:
            ctx.force_stages.update({"outline", "analyze_segments", "reduce_knowledge"})
        else:
            ctx.analysis = result
            for stage, key in (
                ("outline", outline_key),
                ("analyze_segments", segments_key),
                ("reduce_knowledge", reduce_key),
            ):
                ctx.manifest = set_stage_status(ctx.manifest, stage, "complete", cache_key=key)
            _persist(ctx)
            return result

    ctx.manifest = set_stage_status(ctx.manifest, "outline", "running")
    _persist(ctx)
    try:
        result = analyze_evidence_bundle(
            bundle,
            provider=provider,
            cancel_event=ctx.cancel_event,
            output_dir=ctx.workspace.root,
            analysis_mode=ctx.config.analysis.mode,
            native_adapter=resolve_native_adapter(
                analysis_mode=ctx.config.analysis.mode,
                provider_name=ctx.config.analysis.provider,
                max_video_seconds=ctx.config.budget.max_video_seconds,
            ),
        )
    except BudgetExceeded as error:
        raise PipelinePaused(str(error), reason=error.kind) from error
    except Exception as error:  # noqa: BLE001
        _fail_stage(ctx, "outline", f"analysis failed: {error}")

    _write_analysis_cache(ctx, "outline", outline_key, "course-map.json", result.course_map)
    mark_stage_complete(ctx.workspace.root, "outline", outline_key)
    ctx.manifest = set_stage_status(ctx.manifest, "outline", "complete", cache_key=outline_key)

    _write_analysis_cache(ctx, "analyze_segments", segments_key, "segment-manifest.json", result.segments)
    mark_stage_complete(ctx.workspace.root, "analyze_segments", segments_key)
    ctx.manifest = set_stage_status(ctx.manifest, "analyze_segments", "complete", cache_key=segments_key)

    _write_analysis_cache(ctx, "reduce_knowledge", reduce_key, "knowledge-document.json", result.knowledge)
    _complete_stage(ctx, "reduce_knowledge", reduce_key)
    write_analysis_artifacts(result, ctx.workspace.root / "analysis")
    ctx.analysis = result
    return result


def _load_editorial_plan(ctx: RunContext) -> "EditorialPlan | None":
    from yt2class.domain.editorial import EditorialPlan

    path = ctx.workspace.root / "editorial" / "editorial-plan.json"
    if not path.is_file():
        return None
    try:
        return EditorialPlan.model_validate_json(path.read_text(encoding="utf-8"))
    except ValueError:
        raise CacheCorrupt(f"corrupt editorial plan at {path}") from None


def run_editorial_stages(ctx: RunContext, analysis: AnalysisResult, bundle: EvidenceBundle) -> None:
    from yt2class.domain.editorial import EditorialPlan
    from yt2class.domain.verification import VerificationReport

    provider = _provider(ctx)
    knowledge = analysis.knowledge
    transcript = bundle.transcript
    visual = analysis.visual
    course_map = analysis.course_map
    revision = ctx.review_revision()

    plan_key = compute_cache_key(
        "edit_deck",
        config_digest=ctx.config_digest(),
        input_hashes=[
            model_digest(knowledge),
            str(ctx.config.editor.target_pages),
            str(ctx.config.editor.max_pages),
            ctx.config.editor.order,
        ],
        tool_versions=ctx.tool_versions(),
    )
    ctx.manifest = refresh_stage_keys(
        ctx.manifest,
        ctx.workspace.root,
        "edit_deck",
        plan_key,
        save=lambda m: save_manifest(ctx.workspace.root, m),
    )
    plan_path = stage_artifact_path(ctx.workspace.root, "edit_deck", plan_key, "editorial-plan.json")
    on_disk_plan = _load_editorial_plan(ctx)
    plan_regenerated = False

    def _validate_plan(_: Path) -> None:
        load_validated_json(plan_path, EditorialPlan)

    if validated_cache_hit(ctx.workspace.root, "edit_deck", plan_key, _validate_plan):
        cached_plan = load_validated_json(plan_path, EditorialPlan)
        if on_disk_plan is not None and model_digest(on_disk_plan) != model_digest(cached_plan):
            plan = on_disk_plan
        else:
            plan = cached_plan
        ctx.manifest = set_stage_status(ctx.manifest, "edit_deck", "complete", cache_key=plan_key)
        _persist(ctx)
    else:
        ctx.manifest = set_stage_status(ctx.manifest, "edit_deck", "running")
        _persist(ctx)
        if on_disk_plan is not None:
            plan = on_disk_plan
            plan_regenerated = False
        else:
            try:
                plan = plan_deck(
                    knowledge,
                    transcript=transcript,
                    visual=visual,
                    course_map=course_map,
                    provider=provider,
                    target_pages=ctx.config.editor.target_pages,
                    max_pages=ctx.config.editor.max_pages,
                    order=ctx.config.editor.order,
                )
            except BudgetExceeded as error:
                raise PipelinePaused(str(error), reason=error.kind) from error
            plan_regenerated = True
        atomic_write_json(plan_path, plan.model_dump(mode="json"))
        _complete_stage(ctx, "edit_deck", plan_key)

    verify_key = compute_cache_key(
        "verify_claims",
        config_digest=ctx.config_digest(),
        input_hashes=[model_digest(plan), ctx.config.quality.mode],
        tool_versions=ctx.tool_versions(),
    )
    ctx.manifest = refresh_stage_keys(
        ctx.manifest,
        ctx.workspace.root,
        "verify_claims",
        verify_key,
        save=lambda m: save_manifest(ctx.workspace.root, m),
    )
    report_path = stage_artifact_path(
        ctx.workspace.root, "verify_claims", verify_key, "verification-report.json"
    )

    def _validate_report(_: Path) -> None:
        load_validated_json(report_path, VerificationReport)

    if validated_cache_hit(ctx.workspace.root, "verify_claims", verify_key, _validate_report):
        report = load_validated_json(report_path, VerificationReport)
        outcome_plan = plan
        outcome_knowledge = knowledge
        ctx.manifest = set_stage_status(ctx.manifest, "verify_claims", "complete", cache_key=verify_key)
        _persist(ctx)
    else:
        ctx.manifest = set_stage_status(ctx.manifest, "verify_claims", "running")
        _persist(ctx)
        try:
            outcome = verify_plan(
                knowledge,
                plan=plan,
                transcript=transcript,
                visual=visual,
                provider=provider,
                quality_mode=ctx.config.quality.mode,
            )
        except BudgetExceeded as error:
            raise PipelinePaused(str(error), reason=error.kind) from error
        atomic_write_json(report_path, outcome.report.model_dump(mode="json"))
        _complete_stage(ctx, "verify_claims", verify_key)
        report = outcome.report
        if on_disk_plan is not None and model_digest(on_disk_plan) != model_digest(outcome.plan):
            outcome_plan = on_disk_plan
        else:
            outcome_plan = outcome.plan
        outcome_knowledge = outcome.knowledge

    editorial_dir = ctx.workspace.root / "editorial"
    if revision > 0:
        disk_plan = _load_editorial_plan(ctx)
        if disk_plan is not None:
            outcome_plan = disk_plan
    report_disk = editorial_dir / "verification-report.json"
    if report_disk.is_file():
        try:
            on_disk_report = VerificationReport.model_validate_json(report_disk.read_text(encoding="utf-8"))
            if model_digest(on_disk_report) != model_digest(report):
                report = on_disk_report
        except ValueError:
            pass
    should_write_editorial = plan_regenerated
    if should_write_editorial:
        write_editorial_artifacts(
            outcome_plan,
            editorial_dir,
            report=report,
            knowledge=outcome_knowledge,
        )
    if ctx.config.quality.mode == "strict" and report.quality_mode != "strict":
        review_path = editorial_dir / "review.html"
        raise PipelinePaused(
            f"strict quality requires review before delivery: {review_path}",
            reason="review_required",
        )


def run_delivery_stages(ctx: RunContext, bundle: EvidenceBundle, analysis: AnalysisResult) -> Path:
    from yt2class.adapters.render.pptxgenjs import validate_pptx_package
    from yt2class.domain.editorial import EditorialPlan
    from yt2class.domain.verification import VerificationReport
    from yt2class.domain.slide_spec_v3 import SlideSpecV3
    from yt2class.stages.bind_spec import BindResult

    editorial_dir = ctx.workspace.root / "editorial"
    plan = EditorialPlan.model_validate_json((editorial_dir / "editorial-plan.json").read_text(encoding="utf-8"))
    report = VerificationReport.model_validate_json(
        (editorial_dir / "verification-report.json").read_text(encoding="utf-8")
    )
    knowledge = analysis.knowledge
    revision = ctx.review_revision()

    bind_key = compute_cache_key(
        "bind_spec",
        config_digest=ctx.config_digest(),
        input_hashes=[model_digest(plan), model_digest(report), model_digest(knowledge)],
        tool_versions=ctx.tool_versions(),
        review_revision=revision,
    )
    ctx.manifest = refresh_stage_keys(
        ctx.manifest,
        ctx.workspace.root,
        "bind_spec",
        bind_key,
        save=lambda m: save_manifest(ctx.workspace.root, m),
    )
    spec_cache = stage_artifact_path(ctx.workspace.root, "bind_spec", bind_key, "slide-spec.v3.json")
    spec_delivery = ctx.workspace.safe_path("delivery/slide-spec.v3.json")

    def _validate_bind(_: Path) -> None:
        load_validated_json(spec_delivery, SlideSpecV3)

    if validated_cache_hit(ctx.workspace.root, "bind_spec", bind_key, _validate_bind):
        spec = load_validated_json(spec_delivery, SlideSpecV3)
        bind = BindResult(spec=spec, spec_path="delivery/slide-spec.v3.json")
        _complete_stage(ctx, "bind_spec", bind_key)
    else:
        ctx.manifest = set_stage_status(ctx.manifest, "bind_spec", "running")
        _persist(ctx)
        bind = bind_plan_to_spec(
            plan=plan,
            knowledge=knowledge,
            report=report,
            source=bundle.source,
            transcript=bundle.transcript,
            visual=analysis.visual,
            workspace=ctx.workspace,
            analysis_mode=ctx.config.analysis.mode,
        )
        atomic_write_json(spec_cache, bind.spec.model_dump(mode="json"))
        _complete_stage(ctx, "bind_spec", bind_key)

    render_key = compute_cache_key(
        "render",
        config_digest=ctx.config_digest(),
        input_hashes=[bind_key, ctx.config.render.preview, model_digest(bind.spec)],
        tool_versions=ctx.tool_versions(),
        review_revision=revision,
    )
    ctx.manifest = refresh_stage_keys(
        ctx.manifest,
        ctx.workspace.root,
        "render",
        render_key,
        save=lambda m: save_manifest(ctx.workspace.root, m),
    )
    pptx = ctx.workspace.safe_path("delivery/lesson.pptx")

    def _validate_render(_: Path) -> None:
        if not pptx.is_file() or pptx.stat().st_size < 100:
            raise CacheCorrupt("pptx missing or truncated")
        validate_pptx_package(pptx, spec=bind.spec)

    if validated_cache_hit(ctx.workspace.root, "render", render_key, _validate_render):
        _complete_stage(ctx, "render", render_key)
        return pptx

    ctx.manifest = set_stage_status(ctx.manifest, "render", "running")
    _persist(ctx)
    stage = render_spec(
        bind,
        ctx.workspace,
        request_id=f"render-{ctx.manifest.run_id}",
        preview_policy=ctx.config.render.preview,
        output_path="delivery/lesson.pptx",
    )
    if ctx.config.render.preview == "required" and not stage.report.render_complete:
        _fail_stage(ctx, "render", "preview_policy=required but render QA did not pass")
    _complete_stage(ctx, "render", render_key)
    return ctx.workspace.safe_path(stage.pptx_path)


def execute_run(
    output_dir: Path,
    *,
    build: BuildSource,
    config: CourseConfig | None = None,
    run_id: str | None = None,
    resume: bool = False,
    force_stages: set[StageName] | None = None,
    stop_after: StageName | None = None,
    cancel_event: Event | None = None,
    provider: Provider | None = None,
    evidence_bundle: EvidenceBundle | None = None,
) -> RunOutcome:
    cfg = config or CourseConfig()
    workspace_root = output_dir.expanduser()
    existing_manifest = None
    if resume and run_id:
        candidate = workspace_root / run_id
        if candidate.is_dir():
            existing_manifest = load_manifest(candidate)
    rid = run_id or (existing_manifest.run_id if existing_manifest else new_run_id())
    workspace = Workspace.create(workspace_root, run_id=rid)

    manifest = load_manifest(workspace.root) if resume else None
    merged_build, _request_record, inputs_changed = merge_build_for_resume(workspace.root, build)
    build = merged_build
    source_id = build.source_id or (evidence_bundle.source_id if evidence_bundle else "src-pending")
    if manifest is None:
        tools = probe_tool_versions(provider=cfg.analysis.provider)
        manifest = initial_manifest(
            run_id=rid,
            source_id=source_id,
            analysis_mode=cfg.analysis.mode,
            producer_version=tools.producer_version,
        )
        manifest = manifest.model_copy(update={"tool_versions": tools})
    write_desensitized_snapshot(cfg, workspace.root)
    save_run_request(workspace.root, record_from_build(build, review_revision=_request_record.review_revision))

    ctx = RunContext(
        workspace=workspace,
        config=cfg,
        budget=RunBudget(limits=cfg.budget.to_limits()),
        manifest=manifest,
        cancel_event=cancel_event,
        provider=provider,
        force_stages=force_stages or set(),
        stop_after=stop_after,
        build=build,
    )
    if resume:
        _reconcile_resume_cache(ctx)
    if inputs_changed:
        _invalidate_from(ctx, "extract_evidence")

    injected_bundle = evidence_bundle is not None
    if injected_bundle:
        ctx.evidence = evidence_bundle
        ctx.source = evidence_bundle.source
        ctx.manifest = ctx.manifest.model_copy(update={"source_id": evidence_bundle.source_id})
    elif build.video is None and build.url is None:
        raise PipelineError("run requires a source or evidence bundle")

    if injected_bundle:
        source = ctx.source  # type: ignore[assignment]
    else:
        source = run_ingest(ctx, build)

    if stop_after == "ingest":
        return RunOutcome(manifest=ctx.manifest, workspace=workspace)

    if injected_bundle:
        bundle = evidence_bundle  # type: ignore[assignment]
    else:
        bundle = run_extract_evidence(ctx, source)
    if stop_after == "extract_evidence":
        return RunOutcome(manifest=ctx.manifest, workspace=workspace)

    analysis = run_analysis_stages(ctx, bundle)
    if stop_after in {"outline", "analyze_segments", "reduce_knowledge"}:
        return RunOutcome(manifest=ctx.manifest, workspace=workspace)

    run_editorial_stages(ctx, analysis, bundle)
    if stop_after in {"edit_deck", "verify_claims"}:
        return RunOutcome(manifest=ctx.manifest, workspace=workspace)

    pptx = run_delivery_stages(ctx, bundle, analysis)
    return RunOutcome(manifest=ctx.manifest, workspace=workspace, pptx_path=pptx)


def seed_evidence_bundle(workspace: Workspace, bundle: EvidenceBundle) -> Path:
    """Test helper: publish a bundle and mark extract_evidence complete."""

    path = workspace.safe_path("evidence/evidence-bundle.json", create_parent=True)
    atomic_write_json(path, bundle.model_dump(mode="json"))
    cache_key = compute_cache_key(
        "extract_evidence",
        config_digest=CourseConfig().config_digest(),
        input_hashes=[bundle.source_hash],
        tool_versions={"producer_version": "seed", "yt_dlp": None, "ffmpeg": None, "provider": "fake", "prompt": None},
    )
    artifact = stage_artifact_path(workspace.root, "extract_evidence", cache_key, "evidence-bundle.json")
    atomic_write_json(artifact, bundle.model_dump(mode="json"))
    mark_stage_complete(workspace.root, "extract_evidence", cache_key)
    return path


__all__ = [
    "PipelineError",
    "PipelinePaused",
    "RunContext",
    "RunOutcome",
    "execute_run",
    "run_analysis_stages",
    "run_delivery_stages",
    "run_extract_evidence",
    "run_ingest",
    "seed_evidence_bundle",
]

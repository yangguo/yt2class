"""Command-line interface for yt2class."""

from pathlib import Path
from typing import Optional
import json

import typer
from pydantic import ValidationError

from yt2class.domain.course_map import CourseMap
from yt2class.domain.editorial import EditorialPlan
from yt2class.domain.evidence import EvidenceBundle
from yt2class.domain.knowledge import KnowledgeDocument
from yt2class.domain.review import ReviewEdits
from yt2class.domain.transcript import TranscriptDocument
from yt2class.domain.verification import QualityMode, StrictClosureError, VerificationReport
from yt2class.domain.media_audit import MediaPrivacyAudit
from yt2class.domain.slide_spec_v3 import AnalysisMode
from yt2class.domain.visual import VisualCatalogue
from yt2class.inputs import read_urls
from yt2class.orchestration.analyze import analyze_evidence_bundle, write_analysis_artifacts
from yt2class.orchestration.edit import (
    StrictVerificationError,
    build_review,
    load_persisted_knowledge,
    load_persisted_report,
    load_persisted_revision,
    plan_deck,
    verify_plan,
    write_editorial_artifacts,
)
from yt2class.adapters.providers.factory import (
    SUPPORTED_ANALYSIS_PROVIDERS,
    resolve_course_provider,
)
from yt2class.config import AnalysisConfig, BuildSource, CourseConfig, load_config
from yt2class.orchestration.batch import BatchItem, run_batch as run_product_batch
from yt2class.orchestration.doctor import run_doctor
from yt2class.orchestration.manifest_io import load_manifest
from yt2class.orchestration.cancel import install_sigint_handler
from yt2class.orchestration.cache_policy import invalidate_stage_tree
from yt2class.orchestration.delivery import bind_plan_to_spec, render_spec, binder_status_dict, renderer_status_dict
from yt2class.orchestration.pipeline import PipelineError, PipelinePaused, execute_run
from yt2class.orchestration.run_request import bump_review_revision, discover_run_root
from yt2class.orchestration.workspace import Workspace
from yt2class.pipeline import PipelineError as LegacyPipelineError, build_batch
from yt2class.stages.ingest import ingest_source
from yt2class.stages.render import render_bound_spec
from yt2class.stages.bind_spec import BindError, bind_editorial_plan
from yt2class.domain.source import SourceInput
from yt2class.stages.review import apply_review_edits, stub_binder, stub_renderer

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_REVIEW_OR_BUDGET = 2
EXIT_BATCH_PARTIAL = 3

app = typer.Typer(
    help=(
        "Convert course videos into source-faithful PPTX notes. "
        "Exit codes: 0=success, 1=failure, 2=review/budget pause, 3=batch partial failure."
    ),
    no_args_is_help=True,
)


@app.callback()
def main() -> None:
    """Create study notes from one or more course videos."""


@app.command()
def build(
    links: Path = typer.Option(
        ...,
        "--links",
        "-i",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        help="Text file containing one YouTube URL per line.",
    ),
    output: Path = typer.Option(
        Path("output"),
        "--output",
        "-o",
        help="Directory containing restartable lesson run artifacts.",
    ),
    selection_file: Optional[Path] = typer.Option(
        None,
        "--selection-file",
        exists=True,
        file_okay=True,
        dir_okay=False,
        help="Reviewed JSON lesson plan; bypasses the model call.",
    ),
    max_slides: int = typer.Option(
        12,
        min=1,
        max=30,
        help="Maximum number of learning slides per lesson.",
    ),
    force: bool = typer.Option(False, "--force", help="Rebuild completed runs."),
    preview: bool = typer.Option(True, "--preview/--no-preview", help="Write slide previews."),
) -> None:
    """Build PPTX study notes from a batch of course links."""
    try:
        urls = read_urls(links)
        results = build_batch(
            urls,
            output,
            selection_file=selection_file,
            max_slides=max_slides,
            force=force,
            preview=preview,
        )
    except (ValueError, LegacyPipelineError) as error:
        typer.echo(f"Build failed: {error}", err=True)
        raise typer.Exit(code=EXIT_FAIL) from error

    for result in results:
        typer.echo(
            f"OK {result.url} -> {result.pptx_path} "
            f"({result.selected_count}/{result.candidate_count} frames selected)"
        )


@app.command()
def analyze(
    evidence: Path = typer.Option(
        ...,
        "--evidence",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        help="EvidenceBundle JSON produced by extract_evidence.",
    ),
    output: Path = typer.Option(
        Path("output/analysis"),
        "--output",
        "-o",
        help="Directory for CourseMap, SegmentManifest, and KnowledgeDocument.",
    ),
    provider_name: str = typer.Option(
        "fake",
        "--provider",
        help=f"Analysis provider ({', '.join(sorted(SUPPORTED_ANALYSIS_PROVIDERS))}).",
    ),
    mode: AnalysisMode = typer.Option(
        "frames",
        "--mode",
        help="Analysis input mode. Default frames; native-video and hybrid are opt-in.",
    ),
) -> None:
    """Analyze an EvidenceBundle with the configured analysis provider."""

    _reject_unsupported_provider(provider_name, "analyze")
    try:
        bundle = EvidenceBundle.model_validate_json(evidence.read_text(encoding="utf-8"))
        native_adapter = None
        if mode in {"native-video", "hybrid"}:
            from yt2class.adapters.providers.native_video import fake_native_adapter

            native_adapter = fake_native_adapter()
        provider = None
        if provider_name != "fake":
            provider = resolve_course_provider(
                provider_name,
                CourseConfig().analysis,
                run_root=output.parent,
                visual=bundle.visual,
            )
        result = analyze_evidence_bundle(
            bundle,
            output_dir=output,
            analysis_mode=mode,
            native_adapter=native_adapter,
            provider=provider,
        )
        paths = write_analysis_artifacts(result, output)
    except (OSError, ValueError) as error:
        typer.echo(f"Analyze failed: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(
        f"OK {provider_name} analysis -> {paths['knowledge']} "
        f"(coverage={'complete' if result.coverage_complete else 'gaps'}; "
        f"topics={len(result.course_map.topics)}; claims={len(result.knowledge.iter_claims())}; "
        f"mode={mode})"
    )


def _reject_unsupported_provider(provider_name: str, command: str) -> None:
    if provider_name in SUPPORTED_ANALYSIS_PROVIDERS:
        return
    supported = ", ".join(sorted(SUPPORTED_ANALYSIS_PROVIDERS))
    typer.echo(
        f"{command} supports --provider {supported}; got {provider_name!r}.",
        err=True,
    )
    raise typer.Exit(code=2)


def _resolve_cli_provider(
    provider_name: str,
    analysis: AnalysisConfig,
    *,
    run_root: Path | None,
    visual: VisualCatalogue | None,
):
    if provider_name == "fake":
        return None
    return resolve_course_provider(
        provider_name,
        analysis,
        run_root=run_root,
        visual=visual,
    )


@app.command()
def plan(
    knowledge: Path = typer.Option(..., "--knowledge", exists=True, file_okay=True, readable=True),
    transcript: Path = typer.Option(..., "--transcript", exists=True, file_okay=True, readable=True),
    visual: Path = typer.Option(..., "--visual", exists=True, file_okay=True, readable=True),
    output: Path = typer.Option(Path("output/editorial"), "--output", "-o"),
    course_map: Optional[Path] = typer.Option(None, "--course-map", exists=True, file_okay=True, readable=True),
    target_pages: int = typer.Option(12, "--target-pages", min=4, max=100),
    max_pages: int = typer.Option(20, "--max-pages", min=4, max=100),
    order: str = typer.Option("chronological", "--order"),
    provider_name: str = typer.Option("fake", "--provider"),
) -> None:
    """Build an EditorialPlan with FakeProvider. Not a live LLM."""

    _reject_unsupported_provider(provider_name, "plan")
    try:
        doc = KnowledgeDocument.model_validate_json(knowledge.read_text(encoding="utf-8"))
        speech = TranscriptDocument.model_validate_json(transcript.read_text(encoding="utf-8"))
        frames = VisualCatalogue.model_validate_json(visual.read_text(encoding="utf-8"))
        topics = (
            CourseMap.model_validate_json(course_map.read_text(encoding="utf-8"))
            if course_map is not None
            else None
        )
        provider = _resolve_cli_provider(
            provider_name,
            CourseConfig().analysis,
            run_root=output.parent if (output.parent / "evidence").is_dir() else None,
            visual=frames,
        )
        result = plan_deck(
            doc,
            transcript=speech,
            visual=frames,
            course_map=topics,
            target_pages=target_pages,
            max_pages=max_pages,
            order=order,  # type: ignore[arg-type]
            provider=provider,
        )
        paths = write_editorial_artifacts(
            result,
            output,
            knowledge=doc,
            transcript=speech,
            visual=frames,
        )
    except (OSError, ValueError) as error:
        typer.echo(f"Plan failed: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"OK fake plan -> {paths['plan']} (pages={len(result.pages)})")


@app.command()
def verify(
    knowledge: Path = typer.Option(..., "--knowledge", exists=True, file_okay=True, readable=True),
    plan_path: Path = typer.Option(..., "--plan", exists=True, file_okay=True, readable=True),
    transcript: Path = typer.Option(..., "--transcript", exists=True, file_okay=True, readable=True),
    visual: Path = typer.Option(..., "--visual", exists=True, file_okay=True, readable=True),
    output: Path = typer.Option(Path("output/editorial"), "--output", "-o"),
    quality_mode: QualityMode = typer.Option("draft", "--mode"),
    provider_name: str = typer.Option("fake", "--provider"),
) -> None:
    """Verify claims with FakeProvider. Not a live LLM."""

    _reject_unsupported_provider(provider_name, "verify")
    try:
        doc = KnowledgeDocument.model_validate_json(knowledge.read_text(encoding="utf-8"))
        planned = EditorialPlan.model_validate_json(plan_path.read_text(encoding="utf-8"))
        speech = TranscriptDocument.model_validate_json(transcript.read_text(encoding="utf-8"))
        frames = VisualCatalogue.model_validate_json(visual.read_text(encoding="utf-8"))
        provider = _resolve_cli_provider(
            provider_name,
            CourseConfig().analysis,
            run_root=output.parent if (output.parent / "evidence").is_dir() else None,
            visual=frames,
        )
        doc = load_persisted_knowledge(output, doc)
        existing = load_persisted_report(output, doc.source_id)
        outcome = verify_plan(
            doc,
            plan=planned,
            transcript=speech,
            visual=frames,
            quality_mode=quality_mode,
            existing=existing,
            provider=provider,
        )
        paths = write_editorial_artifacts(
            outcome.plan,
            output,
            report=outcome.report,
            knowledge=outcome.knowledge,
            transcript=speech,
            visual=frames,
        )
    except StrictVerificationError as error:
        typer.echo(f"Verify refused strict output: {error}", err=True)
        if error.outcome is not None:
            write_editorial_artifacts(
                error.outcome.plan,
                output,
                report=error.outcome.report,
                knowledge=error.outcome.knowledge,
                transcript=speech,
                visual=frames,
            )
        raise typer.Exit(code=2) from error
    except (OSError, ValueError) as error:
        typer.echo(f"Verify failed: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(
        f"OK fake verify -> {paths['report']} "
        f"(mode={outcome.report.quality_mode}; claims={len(outcome.report.verdicts)})"
    )


@app.command()
def review(
    knowledge: Path = typer.Option(..., "--knowledge", exists=True, file_okay=True, readable=True),
    plan_path: Path = typer.Option(..., "--plan", exists=True, file_okay=True, readable=True),
    report_path: Path = typer.Option(..., "--report", exists=True, file_okay=True, readable=True),
    transcript: Path = typer.Option(..., "--transcript", exists=True, file_okay=True, readable=True),
    visual: Path = typer.Option(..., "--visual", exists=True, file_okay=True, readable=True),
    output: Path = typer.Option(Path("output/editorial"), "--output", "-o"),
    course_map: Optional[Path] = typer.Option(None, "--course-map", exists=True, file_okay=True, readable=True),
    source_url: Optional[str] = typer.Option(None, "--source-url"),
    apply: Optional[Path] = typer.Option(None, "--apply", exists=True, file_okay=True, readable=True),
    run_dir: Optional[Path] = typer.Option(
        None,
        "--run",
        file_okay=False,
        dir_okay=True,
        help="Run workspace for M4 bind/render after --apply.",
    ),
    provider_name: str = typer.Option("fake", "--provider"),
) -> None:
    """Write offline review.html/json, or apply review edits (--apply; use --run for M4 delivery)."""

    _reject_unsupported_provider(provider_name, "review")
    try:
        doc = KnowledgeDocument.model_validate_json(knowledge.read_text(encoding="utf-8"))
        planned = EditorialPlan.model_validate_json(plan_path.read_text(encoding="utf-8"))
        report = VerificationReport.model_validate_json(report_path.read_text(encoding="utf-8"))
        speech = TranscriptDocument.model_validate_json(transcript.read_text(encoding="utf-8"))
        frames = VisualCatalogue.model_validate_json(visual.read_text(encoding="utf-8"))
        topics = (
            CourseMap.model_validate_json(course_map.read_text(encoding="utf-8"))
            if course_map is not None
            else None
        )
        doc = load_persisted_knowledge(output, doc)
        persisted_report = load_persisted_report(output, doc.source_id)
        if persisted_report is not None:
            report = persisted_report
        media_privacy: MediaPrivacyAudit | None = None
        audit_candidates = [
            knowledge.parent / "media-privacy-audit.json",
            output / "media-privacy-audit.json",
            output.parent / "analysis" / "media-privacy-audit.json",
        ]
        for audit_path in audit_candidates:
            if audit_path.is_file():
                media_privacy = MediaPrivacyAudit.model_validate_json(
                    audit_path.read_text(encoding="utf-8")
                )
                break
        bundle = build_review(
            knowledge=doc,
            plan=planned,
            report=report,
            transcript=speech,
            visual=frames,
            course_map=topics,
            source_url=source_url,
            revision=load_persisted_revision(output, doc.source_id),
            media_privacy=media_privacy,
        )
        if apply is not None:
            from yt2class.adapters.providers.synthetic import fake_course_provider
            from yt2class.orchestration.analyze import default_capabilities

            edits = ReviewEdits.model_validate_json(apply.read_text(encoding="utf-8"))
            binder = None
            renderer = None
            resolved_run = (
                run_dir.resolve()
                if run_dir is not None
                else discover_run_root(output, plan_path.parent, Path.cwd())
            )
            if resolved_run is not None:
                run_root = resolved_run
                workspace = Workspace(
                    root=run_root,
                    media_dir=run_root / "media",
                    metadata_dir=run_root / "metadata",
                    tmp_dir=run_root / "tmp",
                    lock_path=run_root / ".write.lock",
                )
                from yt2class.domain.source import SourceManifest

                source = SourceManifest.model_validate_json(
                    (run_root / "metadata" / "source-manifest.json").read_text(encoding="utf-8")
                )

                def _binder(plan_model):
                    nonlocal doc, report, speech, frames, source
                    bind = bind_plan_to_spec(
                        plan=plan_model,
                        knowledge=doc,
                        report=report,
                        source=source,
                        transcript=speech,
                        visual=frames,
                        workspace=workspace,
                    )
                    return binder_status_dict(bind)

                def _renderer(plan_model):
                    bind = bind_plan_to_spec(
                        plan=plan_model,
                        knowledge=doc,
                        report=report,
                        source=source,
                        transcript=speech,
                        visual=frames,
                        workspace=workspace,
                    )
                    stage = render_spec(
                        bind,
                        workspace,
                        request_id=f"review-render-{bundle.revision}",
                        preview_policy="optional",
                    )
                    return renderer_status_dict(stage)

                binder = _binder
                renderer = _renderer
            applied = apply_review_edits(
                bundle,
                edits,
                knowledge=doc,
                transcript=speech,
                visual=frames,
                provider=fake_course_provider(default_capabilities()),
                course_map=topics,
                source_url=source_url,
                binder=binder,
                renderer=renderer,
            )
            bundle = applied.bundle
            planned = applied.plan
            report = applied.report
            doc = applied.knowledge
            if resolved_run is not None:
                bump_review_revision(resolved_run)
                invalidate_stage_tree(resolved_run, "bind_spec")
                output = resolved_run / "editorial"
        paths = write_editorial_artifacts(
            planned,
            output,
            report=report,
            bundle=bundle,
            knowledge=doc,
            transcript=speech,
            visual=frames,
        )
    except StrictClosureError as error:
        typer.echo(f"Review refused strict render: {error}", err=True)
        raise typer.Exit(code=2) from error
    except (OSError, ValueError) as error:
        typer.echo(f"Review failed: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"OK review -> {paths['review_html']} (revision={bundle.revision})")


@app.command("build-run")
def build_run(
    output: Path = typer.Option(Path("runs"), "--output", "-o", help="Runs root directory."),
    run_id: Optional[str] = typer.Option(None, "--run-id", help="Stable run id for resume."),
    config: Optional[Path] = typer.Option(None, "--config", exists=True, readable=True),
    url: Optional[str] = typer.Option(None, "--url"),
    video: Optional[Path] = typer.Option(None, "--video", exists=True, file_okay=True),
    subtitles: Optional[Path] = typer.Option(None, "--subtitles", exists=True, file_okay=True),
    resume: bool = typer.Option(False, "--resume", help="Reuse cached stages from manifest.json."),
) -> None:
    """Run the M5 stage DAG (ingest→evidence→analysis→editorial→bind→render)."""

    if url and video:
        typer.echo("Provide only one of --url or --video.", err=True)
        raise typer.Exit(code=EXIT_FAIL)
    if not url and not video:
        typer.echo("build-run requires --url or --video.", err=True)
        raise typer.Exit(code=EXIT_FAIL)
    cfg = load_config(config)
    build = BuildSource(url=url, video=video, subtitles=subtitles, run_id=run_id)
    cancel = install_sigint_handler()
    try:
        outcome = execute_run(
            output,
            build=build,
            config=cfg,
            run_id=run_id,
            resume=resume,
            cancel_event=cancel,
        )
    except PipelinePaused as error:
        typer.echo(f"Paused: {error}", err=True)
        raise typer.Exit(code=EXIT_REVIEW_OR_BUDGET) from error
    except (PipelineError, BindError, ValidationError) as error:
        typer.echo(f"Run failed: {error}", err=True)
        raise typer.Exit(code=EXIT_FAIL) from error
    pptx = outcome.pptx_path or outcome.workspace.safe_path("delivery/lesson.pptx")
    typer.echo(f"OK run {outcome.manifest.run_id} -> {pptx}")


@app.command()
def batch(
    inputs: Path = typer.Option(..., "--inputs", "-i", exists=True, readable=True),
    output: Path = typer.Option(Path("runs"), "--output", "-o"),
    config: Optional[Path] = typer.Option(None, "--config", exists=True, readable=True),
    continue_on_error: bool = typer.Option(True, "--continue-on-error/--fail-fast"),
) -> None:
    """Batch M5 runs from a text file (one local video path or URL per line)."""

    cfg = load_config(config)
    lines = [line.strip() for line in inputs.read_text(encoding="utf-8").splitlines() if line.strip()]
    items: list[BatchItem] = []
    for line in lines:
        if line.startswith("http"):
            items.append(BatchItem(label=line, build=BuildSource(url=line)))
        else:
            items.append(BatchItem(label=line, build=BuildSource(video=Path(line))))
    cancel = install_sigint_handler()
    report = run_product_batch(
        output,
        items,
        config=cfg,
        continue_on_error=continue_on_error,
        cancel_event=cancel,
    )
    for row in report.results:
        if row.ok:
            typer.echo(f"OK {row.label} -> run {row.run_id}")
        else:
            typer.echo(f"FAIL {row.label}: {row.error}", err=True)
    if not report.all_ok:
        raise typer.Exit(code=EXIT_BATCH_PARTIAL if report.any_ok else EXIT_FAIL)


@app.command()
def ingest(
    video: Optional[Path] = typer.Option(None, "--video", exists=True, file_okay=True),
    url: Optional[str] = typer.Option(None, "--url"),
    output: Path = typer.Option(Path("runs"), "--output", "-o"),
    run_id: Optional[str] = typer.Option(None, "--run-id"),
) -> None:
    """Ingest one source into a run workspace (metadata/source-manifest.json)."""

    if bool(video) == bool(url):
        typer.echo("Provide exactly one of --video or --url.", err=True)
        raise typer.Exit(code=EXIT_FAIL)
    from yt2class.orchestration.manifest_io import new_run_id

    rid = run_id or new_run_id()
    workspace = Workspace.create(output, run_id=rid)
    source = SourceInput.from_value(url if url else video)
    try:
        result = ingest_source(source, workspace)
    except Exception as error:  # noqa: BLE001
        typer.echo(f"Ingest failed: {error}", err=True)
        raise typer.Exit(code=EXIT_FAIL) from error
    typer.echo(f"OK ingest -> {result.manifest_path}")


@app.command()
def resume(
    run: Path = typer.Option(..., "--run", file_okay=False, dir_okay=True),
    config: Optional[Path] = typer.Option(None, "--config", exists=True, readable=True),
) -> None:
    """Resume a run from manifest.json under the run directory."""

    run_dir = run.resolve()
    manifest = load_manifest(run_dir)
    if manifest is None:
        typer.echo(f"No manifest.json in {run_dir}", err=True)
        raise typer.Exit(code=EXIT_FAIL)
    cfg = load_config(config)
    build = BuildSource(source_id=manifest.source_id, run_id=manifest.run_id)
    cancel = install_sigint_handler()
    try:
        outcome = execute_run(
            run_dir.parent,
            build=build,
            config=cfg,
            run_id=manifest.run_id,
            resume=True,
            cancel_event=cancel,
        )
    except PipelinePaused as error:
        typer.echo(f"Paused: {error}", err=True)
        raise typer.Exit(code=EXIT_REVIEW_OR_BUDGET) from error
    except (PipelineError, BindError, ValidationError) as error:
        typer.echo(f"Resume failed: {error}", err=True)
        raise typer.Exit(code=EXIT_FAIL) from error
    pptx = outcome.pptx_path or run_dir / "delivery" / "lesson.pptx"
    typer.echo(f"OK resume -> {pptx}")


@app.command()
def render(
    run: Path = typer.Option(..., "--run", exists=True, file_okay=False, dir_okay=True),
    spec: Optional[Path] = typer.Option(None, "--spec", exists=True, readable=True),
    preview: str = typer.Option("optional", "--preview", help="off|optional|required"),
) -> None:
    """Render delivery/lesson.pptx from a bound SlideSpec in the run workspace."""

    run_dir = run.resolve()
    workspace = Workspace(
        root=run_dir,
        media_dir=run_dir / "media",
        metadata_dir=run_dir / "metadata",
        tmp_dir=run_dir / "tmp",
        lock_path=run_dir / ".write.lock",
    )
    spec_path = spec or (run_dir / "delivery" / "slide-spec.v3.json")
    if not spec_path.is_file():
        typer.echo(f"SlideSpec not found: {spec_path}", err=True)
        raise typer.Exit(code=EXIT_FAIL)
    from yt2class.domain.editorial import EditorialPlan
    from yt2class.domain.knowledge import KnowledgeDocument
    from yt2class.domain.transcript import TranscriptDocument
    from yt2class.domain.verification import VerificationReport
    from yt2class.domain.visual import VisualCatalogue
    from yt2class.domain.source import SourceManifest

    editorial = run_dir / "editorial"
    try:
        plan = EditorialPlan.model_validate_json((editorial / "editorial-plan.json").read_text(encoding="utf-8"))
        report = VerificationReport.model_validate_json(
            (editorial / "verification-report.json").read_text(encoding="utf-8")
        )
        knowledge = KnowledgeDocument.model_validate_json((editorial / "knowledge.json").read_text(encoding="utf-8"))
        transcript = TranscriptDocument.model_validate_json(
            (run_dir / "evidence" / "transcript.json").read_text(encoding="utf-8")
        )
        visual = VisualCatalogue.model_validate_json((run_dir / "evidence" / "visual.json").read_text(encoding="utf-8"))
        source = SourceManifest.model_validate_json(
            (run_dir / "metadata" / "source-manifest.json").read_text(encoding="utf-8")
        )
        bind = bind_editorial_plan(
            plan=plan,
            knowledge=knowledge,
            report=report,
            source=source,
            transcript=transcript,
            visual=visual,
            workspace=workspace,
        )
        stage = render_bound_spec(
            bind,
            workspace,
            request_id=f"cli-render-{manifest.run_id if (manifest := load_manifest(run_dir)) else 'adhoc'}",
            preview_policy=preview,  # type: ignore[arg-type]
        )
    except Exception as error:  # noqa: BLE001
        typer.echo(f"Render failed: {error}", err=True)
        raise typer.Exit(code=EXIT_FAIL) from error
    typer.echo(f"OK render -> {workspace.safe_path(stage.pptx_path)}")


@app.command()
def doctor(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable report."),
) -> None:
    """Check ffmpeg, yt-dlp, Node renderer bundle, preview, and fonts."""

    report = run_doctor()
    if json_output:
        typer.echo(json.dumps(report.to_json(), ensure_ascii=False, indent=2))
    else:
        for check in report.checks:
            typer.echo(f"{check.status.upper():4} {check.name}: {check.detail}")
    raise typer.Exit(code=EXIT_OK if report.ok else EXIT_FAIL)

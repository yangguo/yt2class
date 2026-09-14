"""Command-line interface for yt2class."""

from pathlib import Path
from typing import Optional

import typer

from yt2class.domain.course_map import CourseMap
from yt2class.domain.editorial import EditorialPlan
from yt2class.domain.evidence import EvidenceBundle
from yt2class.domain.knowledge import KnowledgeDocument
from yt2class.domain.review import ReviewEdits
from yt2class.domain.transcript import TranscriptDocument
from yt2class.domain.verification import QualityMode, StrictClosureError, VerificationReport
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
from yt2class.pipeline import PipelineError, build_batch
from yt2class.stages.review import apply_review_edits, stub_binder, stub_renderer

app = typer.Typer(
    help="Batch-convert YouTube course videos into source-faithful PPTX notes.",
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
    except (ValueError, PipelineError) as error:
        typer.echo(f"Build failed: {error}", err=True)
        raise typer.Exit(code=1) from error

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
        help="Only the tested FakeProvider path is available ('fake').",
    ),
) -> None:
    """Analyze an EvidenceBundle with FakeProvider. Not a live LLM."""

    if provider_name != "fake":
        typer.echo(
            "analyze only supports --provider fake; live models are opt-in via tests/live.",
            err=True,
        )
        raise typer.Exit(code=2)
    try:
        bundle = EvidenceBundle.model_validate_json(evidence.read_text(encoding="utf-8"))
        result = analyze_evidence_bundle(bundle, output_dir=output)
        paths = write_analysis_artifacts(result, output)
    except (OSError, ValueError) as error:
        typer.echo(f"Analyze failed: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(
        f"OK fake analysis -> {paths['knowledge']} "
        f"(coverage={'complete' if result.coverage_complete else 'gaps'}; "
        f"topics={len(result.course_map.topics)}; claims={len(result.knowledge.iter_claims())})"
    )


def _reject_live_provider(provider_name: str, command: str) -> None:
    if provider_name != "fake":
        typer.echo(
            f"{command} only supports --provider fake; live models are opt-in via tests/live.",
            err=True,
        )
        raise typer.Exit(code=2)


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

    _reject_live_provider(provider_name, "plan")
    try:
        doc = KnowledgeDocument.model_validate_json(knowledge.read_text(encoding="utf-8"))
        speech = TranscriptDocument.model_validate_json(transcript.read_text(encoding="utf-8"))
        frames = VisualCatalogue.model_validate_json(visual.read_text(encoding="utf-8"))
        topics = (
            CourseMap.model_validate_json(course_map.read_text(encoding="utf-8"))
            if course_map is not None
            else None
        )
        result = plan_deck(
            doc,
            transcript=speech,
            visual=frames,
            course_map=topics,
            target_pages=target_pages,
            max_pages=max_pages,
            order=order,  # type: ignore[arg-type]
        )
        paths = write_editorial_artifacts(result, output)
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

    _reject_live_provider(provider_name, "verify")
    try:
        doc = KnowledgeDocument.model_validate_json(knowledge.read_text(encoding="utf-8"))
        planned = EditorialPlan.model_validate_json(plan_path.read_text(encoding="utf-8"))
        speech = TranscriptDocument.model_validate_json(transcript.read_text(encoding="utf-8"))
        frames = VisualCatalogue.model_validate_json(visual.read_text(encoding="utf-8"))
        doc = load_persisted_knowledge(output, doc)
        existing = load_persisted_report(output, doc.source_id)
        outcome = verify_plan(
            doc,
            plan=planned,
            transcript=speech,
            visual=frames,
            quality_mode=quality_mode,
            existing=existing,
        )
        paths = write_editorial_artifacts(
            outcome.plan, output, report=outcome.report, knowledge=outcome.knowledge
        )
    except StrictVerificationError as error:
        typer.echo(f"Verify refused strict output: {error}", err=True)
        if error.outcome is not None:
            write_editorial_artifacts(
                error.outcome.plan,
                output,
                report=error.outcome.report,
                knowledge=error.outcome.knowledge,
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
    provider_name: str = typer.Option("fake", "--provider"),
) -> None:
    """Write offline review.html/json, or apply a controlled review edit file."""

    _reject_live_provider(provider_name, "review")
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
        bundle = build_review(
            knowledge=doc,
            plan=planned,
            report=report,
            transcript=speech,
            visual=frames,
            course_map=topics,
            source_url=source_url,
            revision=load_persisted_revision(output, doc.source_id),
        )
        if apply is not None:
            from yt2class.adapters.providers.synthetic import fake_course_provider
            from yt2class.orchestration.analyze import default_capabilities

            edits = ReviewEdits.model_validate_json(apply.read_text(encoding="utf-8"))
            applied = apply_review_edits(
                bundle,
                edits,
                knowledge=doc,
                transcript=speech,
                visual=frames,
                provider=fake_course_provider(default_capabilities()),
                course_map=topics,
                source_url=source_url,
                binder=stub_binder,
                renderer=stub_renderer,
            )
            bundle = applied.bundle
            planned = applied.plan
            report = applied.report
            doc = applied.knowledge
        paths = write_editorial_artifacts(
            planned, output, report=report, bundle=bundle, knowledge=doc
        )
    except StrictClosureError as error:
        typer.echo(f"Review refused strict render: {error}", err=True)
        raise typer.Exit(code=2) from error
    except (OSError, ValueError) as error:
        typer.echo(f"Review failed: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"OK review -> {paths['review_html']} (revision={bundle.revision})")

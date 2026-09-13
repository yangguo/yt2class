"""Command-line interface for yt2class."""

from pathlib import Path
from typing import Optional

import typer

from yt2class.domain.evidence import EvidenceBundle
from yt2class.inputs import read_urls
from yt2class.orchestration.analyze import analyze_evidence_bundle, write_analysis_artifacts
from yt2class.pipeline import PipelineError, build_batch

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

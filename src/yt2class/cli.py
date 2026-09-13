"""Command-line interface for yt2class."""

from pathlib import Path
from typing import Optional

import typer

from yt2class.inputs import read_urls
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

"""LibreOffice → PDF → PNG preview pipeline when tools are available."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from yt2class.adapters.process import ProcessTimedOut, ProcessUnavailable, run_process

PREVIEW_TIMEOUT_SECONDS = 180.0


@dataclass(frozen=True)
class PreviewResult:
    available: bool
    png_paths: list[Path]
    contact_sheet: Path | None
    reason: str | None = None


def _which(name: str) -> str | None:
    return os.getenv(f"YT2CLASS_{name.upper()}") or shutil.which(name)


def libreoffice_binary() -> str | None:
    return _which("libreoffice") or _which("soffice")


def _user_installation_arg(profile_dir: Path) -> str:
    uri = profile_dir.resolve().as_posix()
    if not uri.startswith("/"):
        uri = f"/{uri}"
    return f"--env:UserInstallation=file://{uri}"


def _run_libreoffice(
    binary: str,
    args: list[str],
    *,
    profile_dir: Path,
) -> subprocess.CompletedProcess[str]:
    command = [binary, _user_installation_arg(profile_dir), "--headless", *args]
    try:
        result = run_process(
            command,
            timeout_seconds=PREVIEW_TIMEOUT_SECONDS,
        )
    except ProcessTimedOut as error:
        raise PreviewConversionError(str(error)) from error
    except ProcessUnavailable as error:
        raise PreviewConversionError(str(error)) from error
    return subprocess.CompletedProcess(
        args=command,
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )


class PreviewConversionError(RuntimeError):
    """Raised when preview conversion cannot start or finish."""


def _convert_pptx_to_pdf(pptx_path: Path, work_dir: Path, *, binary: str, profile_dir: Path) -> Path:
    completed = _run_libreoffice(
        binary,
        ["--convert-to", "pdf", "--outdir", str(work_dir), str(pptx_path)],
        profile_dir=profile_dir,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise PreviewConversionError(f"libreoffice pdf export failed: {detail}")
    pdfs = sorted(work_dir.glob("*.pdf"))
    if not pdfs:
        raise PreviewConversionError("libreoffice produced no PDF output")
    return pdfs[0]


def _rasterize_pdf(pdf_path: Path, output_dir: Path, *, prefix: str = "slide") -> list[Path]:
    pdftoppm = _which("pdftoppm")
    if pdftoppm is None:
        raise PreviewConversionError("pdftoppm is not available for multi-page preview")
    stem = output_dir / prefix
    command = [
        pdftoppm,
        "-png",
        "-r",
        "150",
        str(pdf_path),
        str(stem),
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise PreviewConversionError(f"pdftoppm failed: {detail}")
    def _page_index(path: Path) -> int:
        match = re.search(r"-(\d+)\.png$", path.name)
        return int(match.group(1)) if match else 0

    generated = sorted(output_dir.glob(f"{prefix}-*.png"), key=_page_index)
    if not generated:
        raise PreviewConversionError("pdftoppm produced no PNG output")
    renamed: list[Path] = []
    for index, src in enumerate(generated, start=1):
        dest = output_dir / f"slide-{index:03d}.png"
        if src != dest:
            shutil.copy2(src, dest)
            src.unlink(missing_ok=True)
        renamed.append(dest)
    return renamed


def _convert_pptx_direct_png(
    pptx_path: Path,
    work_dir: Path,
    *,
    binary: str,
    profile_dir: Path,
) -> list[Path]:
    completed = _run_libreoffice(
        binary,
        ["--convert-to", "png", "--outdir", str(work_dir), str(pptx_path)],
        profile_dir=profile_dir,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise PreviewConversionError(f"libreoffice png export failed: {detail}")
    generated = sorted(work_dir.glob("*.png"))
    if not generated:
        raise PreviewConversionError("libreoffice produced no PNG output")
    return generated


def convert_pptx_to_pngs(pptx_path: Path, output_dir: Path) -> PreviewResult:
    binary = libreoffice_binary()
    if not binary:
        return PreviewResult(available=False, png_paths=[], contact_sheet=None, reason="libreoffice missing")
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        work_dir = Path(tmp)
        profile_dir = work_dir / "lo-profile"
        profile_dir.mkdir()
        try:
            pdf_path = _convert_pptx_to_pdf(pptx_path, work_dir, binary=binary, profile_dir=profile_dir)
            generated = _rasterize_pdf(pdf_path, output_dir)
        except PreviewConversionError:
            try:
                raw = _convert_pptx_direct_png(pptx_path, work_dir, binary=binary, profile_dir=profile_dir)
            except PreviewConversionError as error:
                return PreviewResult(
                    available=False,
                    png_paths=[],
                    contact_sheet=None,
                    reason=str(error),
                )
            png_paths: list[Path] = []
            for index, src in enumerate(raw):
                dest = output_dir / f"slide-{index + 1:03d}.png"
                shutil.copy2(src, dest)
                png_paths.append(dest)
        else:
            png_paths = generated

        contact = output_dir / "contact-sheet.png"
        if png_paths:
            shutil.copy2(png_paths[0], contact)
        return PreviewResult(
            available=True,
            png_paths=png_paths,
            contact_sheet=contact if contact.exists() else None,
        )


__all__ = [
    "PreviewConversionError",
    "PreviewResult",
    "PREVIEW_TIMEOUT_SECONDS",
    "convert_pptx_to_pngs",
    "libreoffice_binary",
]

"""LibreOffice → PDF → PNG preview pipeline when tools are available."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


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


def convert_pptx_to_pngs(pptx_path: Path, output_dir: Path) -> PreviewResult:
    binary = libreoffice_binary()
    if not binary:
        return PreviewResult(available=False, png_paths=[], contact_sheet=None, reason="libreoffice missing")
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        cmd = [
            binary,
            "--headless",
            "--convert-to",
            "png",
            "--outdir",
            tmp,
            str(pptx_path),
        ]
        completed = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            return PreviewResult(
                available=False,
                png_paths=[],
                contact_sheet=None,
                reason=f"libreoffice failed: {detail}",
            )
        generated = sorted(Path(tmp).glob("*.png"))
        if not generated:
            return PreviewResult(
                available=False,
                png_paths=[],
                contact_sheet=None,
                reason="libreoffice produced no PNG output",
            )
        png_paths: list[Path] = []
        for index, src in enumerate(generated):
            dest = output_dir / f"slide-{index + 1:03d}.png"
            shutil.copy2(src, dest)
            png_paths.append(dest)
        contact = output_dir / "contact-sheet.png"
        if len(png_paths) == 1:
            shutil.copy2(png_paths[0], contact)
        return PreviewResult(available=True, png_paths=png_paths, contact_sheet=contact if contact.exists() else None)


__all__ = ["PreviewResult", "convert_pptx_to_pngs", "libreoffice_binary"]

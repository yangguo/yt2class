"""Algorithmic visual QA over preview PNGs or PPTX metadata."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from yt2class.adapters.render.base import PreviewPolicy
from yt2class.adapters.render.preview import convert_pptx_to_pngs
from yt2class.domain.render_report import VisualQa
from yt2class.domain.slide_spec_v3 import SlideSpecV3

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None  # type: ignore


@dataclass(frozen=True)
class QaOutcome:
    visual_qa: VisualQa
    layout_issues: list[str]


def _analyze_png(path: Path, *, min_font_pt: float) -> list[str]:
    if Image is None:
        return []
    issues: list[str] = []
    with Image.open(path) as image:
        gray = image.convert("L")
        pixels = list(gray.getdata())
        if max(pixels) < 250:
            issues.append(f"{path.name}: possible blank slide")
        if min(pixels) > 5:
            issues.append(f"{path.name}: low contrast; check text visibility")
    if min_font_pt < 10:
        issues.append(f"{path.name}: min_font_pt below readable threshold")
    return issues


def run_visual_qa(
    pptx_path: Path,
    *,
    spec: SlideSpecV3,
    preview_policy: PreviewPolicy,
    run_root: Path,
) -> QaOutcome:
    preview_dir = run_root / "delivery" / "preview"
    preview = convert_pptx_to_pngs(pptx_path, preview_dir)
    if not preview.available:
        if preview_policy == "required":
            return QaOutcome(
                visual_qa="failed",
                layout_issues=[preview.reason or "preview unavailable"],
            )
        return QaOutcome(visual_qa="unavailable", layout_issues=[])

    issues: list[str] = []
    if len(preview.png_paths) != len(spec.slides):
        issues.append(
            f"preview page count {len(preview.png_paths)} != spec {len(spec.slides)}"
        )
    for png in preview.png_paths:
        issues.extend(_analyze_png(png, min_font_pt=spec.render_policy.min_font_pt))

    if issues:
        return QaOutcome(visual_qa="failed", layout_issues=issues)
    return QaOutcome(visual_qa="passed", layout_issues=[])


__all__ = ["QaOutcome", "run_visual_qa"]

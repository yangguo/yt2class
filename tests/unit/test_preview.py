from __future__ import annotations

from pathlib import Path

from yt2class.adapters.render import preview as preview_mod


def test_convert_pptx_to_pngs_uses_pdf_rasterization(tmp_path: Path, monkeypatch):
    pptx = tmp_path / "deck.pptx"
    pptx.write_bytes(b"fake")
    out = tmp_path / "preview"

    def fake_pdf(pptx_path: Path, work_dir: Path, *, binary: str, profile_dir: Path) -> Path:
        pdf = work_dir / "deck.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        return pdf

    def fake_rasterize(pdf_path: Path, output_dir: Path, *, prefix: str = "slide") -> list[Path]:
        first = output_dir / "slide-001.png"
        second = output_dir / "slide-002.png"
        first.write_bytes(b"png1")
        second.write_bytes(b"png2")
        return [first, second]

    monkeypatch.setattr(preview_mod, "libreoffice_binary", lambda: "/bin/soffice")
    monkeypatch.setattr(preview_mod, "_convert_pptx_to_pdf", fake_pdf)
    monkeypatch.setattr(preview_mod, "_rasterize_pdf", fake_rasterize)

    result = preview_mod.convert_pptx_to_pngs(pptx, out)
    assert result.available
    assert len(result.png_paths) == 2
    assert result.contact_sheet is not None

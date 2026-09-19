from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from yt2class.adapters.ocr import (
    OCRCancelled,
    OCRContractError,
    OCRStatus,
    build_ocr_command,
    resolve_ocr_engine,
    run_ocr,
)
from yt2class.adapters.process import ProcessCancelled


def test_resolve_ocr_engine_auto_uses_tesseract_when_present(monkeypatch):
    monkeypatch.setattr("yt2class.adapters.ocr.shutil.which", lambda name: "/bin/tesseract" if name == "tesseract" else None)
    engine, reason = resolve_ocr_engine("auto")
    assert engine == "tesseract"
    assert reason is None


def test_resolve_ocr_engine_auto_reports_missing_tesseract(monkeypatch):
    monkeypatch.setattr("yt2class.adapters.ocr.shutil.which", lambda _name: None)
    engine, reason = resolve_ocr_engine("auto")
    assert engine == "none"
    assert reason and "tesseract" in reason


def test_tesseract_command_includes_language_pack():
    command = build_ocr_command(Path("frame.jpg"), engine="tesseract", languages="jpn+eng")
    assert "-l" in command
    assert "jpn+eng" in command
    assert command[-1] == "tsv"


def test_ocr_runner_binds_regions_to_same_asset_and_occurrence(tmp_path: Path):
    image = tmp_path / "frame.jpg"
    image.write_bytes(b"image")

    def runner(command, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "engine": "fake-ocr",
                    "regions": [
                        {
                            "text": "标题",
                            "bbox": {"x": 1, "y": 2, "width": 30, "height": 10},
                            "confidence": 0.95,
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            stderr="",
        )

    result = run_ocr(
        image,
        asset_id="asset-1",
        occurrence_id="occ-1",
        engine="fake",
        runner=runner,
    )
    assert result.status == OCRStatus.COMPLETE
    assert result.regions[0].asset_id == "asset-1"
    assert result.regions[0].parent_occurrence_id == "occ-1"
    assert result.regions[0].text == "标题"


def test_ocr_unavailable_is_a_gap_not_a_fake_success(tmp_path: Path):
    image = tmp_path / "frame.jpg"
    image.write_bytes(b"image")
    result = run_ocr(image, asset_id="asset-1", occurrence_id="occ-1", engine="none")
    assert result.status == OCRStatus.UNAVAILABLE
    assert result.regions == []
    assert result.error


def test_ocr_rejects_invalid_bbox(tmp_path: Path):
    image = tmp_path / "frame.jpg"
    image.write_bytes(b"image")

    def runner(command, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "regions": [
                        {
                            "text": "bad",
                            "bbox": {"x": 1, "y": 2, "width": 0, "height": 10},
                            "confidence": 0.5,
                        }
                    ]
                }
            ),
            stderr="",
        )

    with pytest.raises(OCRContractError, match="bbox"):
        run_ocr(image, asset_id="asset-1", occurrence_id="occ-1", engine="fake", runner=runner)


def test_tesseract_tsv_output_is_normalized_to_regions(tmp_path: Path):
    image = tmp_path / "frame.jpg"
    image.write_bytes(b"image")

    def runner(command, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=(
                "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\t"
                "top\twidth\theight\tconf\ttext\n"
                "5\t1\t1\t1\t1\t1\t2\t3\t40\t12\t87.5\tHello\n"
            ),
            stderr="",
        )

    result = run_ocr(image, asset_id="asset-1", occurrence_id="occ-1", engine="tesseract", runner=runner)
    assert result.status == OCRStatus.COMPLETE
    assert result.regions[0].text == "Hello"
    assert result.regions[0].bbox.width == 40


def test_ocr_cancellation_is_not_reported_as_a_degraded_success(tmp_path: Path):
    image = tmp_path / "frame.jpg"
    image.write_bytes(b"image")

    def runner(command, **kwargs):
        raise ProcessCancelled("cancelled OCR")

    with pytest.raises(OCRCancelled, match="cancelled OCR"):
        run_ocr(
            image,
            asset_id="asset-1",
            occurrence_id="occ-1",
            engine="fake",
            runner=runner,
        )

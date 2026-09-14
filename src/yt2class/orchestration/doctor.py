"""Environment checks for tools, codecs, renderer bundle, and preview backends."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import shutil
from pathlib import Path
from typing import Any, Literal

from yt2class.adapters.render.base import RenderError

CheckStatus = Literal["ok", "warn", "fail"]


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: CheckStatus
    detail: str


@dataclass
class DoctorReport:
    checks: list[DoctorCheck]

    @property
    def ok(self) -> bool:
        return all(item.status != "fail" for item in self.checks)

    def to_json(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checks": [
                {"name": item.name, "status": item.status, "detail": item.detail}
                for item in self.checks
            ],
        }


def _check_required(name: str, binary: str) -> DoctorCheck:
    path = shutil.which(binary)
    if path:
        return DoctorCheck(name=name, status="ok", detail=path)
    return DoctorCheck(name=name, status="fail", detail=f"{binary} not found on PATH")


def _check_optional(name: str, binary: str) -> DoctorCheck:
    path = shutil.which(binary)
    if path:
        return DoctorCheck(name=name, status="ok", detail=path)
    return DoctorCheck(name=name, status="warn", detail=f"{binary} not found on PATH")


def _safe_renderer_root() -> tuple[Path | None, str | None]:
    env = os.getenv("YT2CLASS_RENDERER_ROOT")
    if env:
        candidate = Path(env).expanduser()
        if (candidate / "package.json").is_file():
            return candidate, None
        return None, f"YT2CLASS_RENDERER_ROOT is invalid: {candidate}"
    try:
        from yt2class.adapters.render.pptxgenjs import renderer_root

        return renderer_root(), None
    except RenderError as error:
        return None, str(error)


def run_doctor() -> DoctorReport:
    checks: list[DoctorCheck] = [
        _check_required("ffmpeg", "ffmpeg"),
        _check_required("ffprobe", "ffprobe"),
        _check_required("node", "node"),
        _check_optional("yt-dlp", "yt-dlp"),
    ]
    root, renderer_error = _safe_renderer_root()
    if renderer_error:
        checks.append(DoctorCheck("pptxgenjs_renderer", "fail", renderer_error))
    elif root is not None and (root / "node_modules" / "pptxgenjs").is_dir():
        checks.append(DoctorCheck("pptxgenjs_renderer", "ok", str(root)))
    else:
        checks.append(
            DoctorCheck(
                "pptxgenjs_renderer",
                "fail",
                f"renderer bundle missing under {root}",
            )
        )
    preview = shutil.which("convert") or shutil.which("magick")
    if preview:
        checks.append(DoctorCheck("preview_imagemagick", "ok", preview))
    else:
        checks.append(
            DoctorCheck(
                "preview_imagemagick",
                "warn",
                "ImageMagick not found; preview thumbnails may be skipped",
            )
        )
    checks.append(
        DoctorCheck(
            "asr",
            "warn",
            "ASR (WhisperX) not probed in doctor; optional extra / worker env",
        )
    )
    font_dirs = [Path("/usr/share/fonts"), Path.home() / ".local" / "share" / "fonts"]
    if any(path.is_dir() for path in font_dirs):
        checks.append(DoctorCheck("fonts", "ok", "system font directories present"))
    else:
        checks.append(DoctorCheck("fonts", "warn", "no standard font directories detected"))
    return DoctorReport(checks=checks)


def write_doctor_report(path: Path) -> DoctorReport:
    report = run_doctor()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.to_json(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


__all__ = ["DoctorCheck", "DoctorReport", "run_doctor", "write_doctor_report"]

"""Renderer adapters. They consume a bound SlideSpec and do not invent pages."""

from yt2class.adapters.render.base import (
    FakeRenderer,
    RenderError,
    RenderRequest,
    Renderer,
    RequestFingerprintConflict,
)
from yt2class.domain.render_report import RenderReport

__all__ = [
    "FakeRenderer",
    "RenderError",
    "RenderRequest",
    "RenderReport",
    "Renderer",
    "RequestFingerprintConflict",
]

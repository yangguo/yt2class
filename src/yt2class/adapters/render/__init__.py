"""Renderer adapters. They consume a bound SlideSpec and do not invent pages."""

from yt2class.adapters.render.base import FakeRenderer, RenderRequest, Renderer
from yt2class.domain.render_report import RenderReport

__all__ = ["FakeRenderer", "RenderRequest", "RenderReport", "Renderer"]

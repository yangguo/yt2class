"""Resolve analysis providers for pipeline and stage CLIs."""

from __future__ import annotations

from pathlib import Path

from yt2class.adapters.providers.base import Provider
from yt2class.adapters.providers.openrouter import OpenRouterProvider
from yt2class.adapters.providers.synthetic import fake_course_provider
from yt2class.adapters.providers.volcengine_ark_plan import VolcengineArkPlanProvider
from yt2class.config import AnalysisConfig
from yt2class.domain.visual import VisualCatalogue
from yt2class.orchestration.analyze import default_capabilities

SUPPORTED_ANALYSIS_PROVIDERS = frozenset({"fake", "openrouter", "ark-plan", "volcengine"})
_ARK_PLAN_ALIASES = frozenset({"ark-plan", "volcengine"})


class UnsupportedAnalysisProvider(ValueError):
    """Raised when config names a provider that is not wired on the product path."""


def resolve_course_provider(
    provider_name: str,
    analysis: AnalysisConfig,
    *,
    run_root: Path | None = None,
    visual: VisualCatalogue | None = None,
) -> Provider:
    if provider_name == "fake":
        return fake_course_provider(default_capabilities())
    if provider_name == "openrouter":
        provider = OpenRouterProvider.from_config(analysis)
        if run_root is not None:
            provider.bind_run_context(run_root, visual=visual)
        return provider
    if provider_name in _ARK_PLAN_ALIASES:
        provider = VolcengineArkPlanProvider.from_config(analysis)
        if run_root is not None:
            provider.bind_run_context(run_root, visual=visual)
        return provider
    raise UnsupportedAnalysisProvider(
        f"unsupported analysis provider {provider_name!r}; "
        f"supported: {', '.join(sorted(SUPPORTED_ANALYSIS_PROVIDERS))}"
    )


__all__ = [
    "SUPPORTED_ANALYSIS_PROVIDERS",
    "UnsupportedAnalysisProvider",
    "resolve_course_provider",
]

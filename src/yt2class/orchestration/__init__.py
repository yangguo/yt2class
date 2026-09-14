"""Run-scoped orchestration helpers."""

from __future__ import annotations

from typing import Any

from yt2class.orchestration.scheduler import (
    SchedulerConfig,
    SchedulerError,
    schedule_windows,
    set_window_status,
)
from yt2class.orchestration.workspace import (
    Workspace,
    WorkspaceBusy,
    WorkspaceError,
    WorkspacePathError,
)

_LAZY_EXPORTS = {
    "AnalysisResult": "yt2class.orchestration.analyze",
    "analyze_course": "yt2class.orchestration.analyze",
    "analyze_evidence_bundle": "yt2class.orchestration.analyze",
    "default_capabilities": "yt2class.orchestration.analyze",
    "write_analysis_artifacts": "yt2class.orchestration.analyze",
    "resolve_native_adapter": "yt2class.orchestration.analyze",
    "build_review": "yt2class.orchestration.edit",
    "plan_deck": "yt2class.orchestration.edit",
    "verify_plan": "yt2class.orchestration.edit",
}


def __getattr__(name: str) -> Any:
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module = importlib.import_module(target)
    return getattr(module, name)


__all__ = [
    "AnalysisResult",
    "SchedulerConfig",
    "SchedulerError",
    "Workspace",
    "WorkspaceBusy",
    "WorkspaceError",
    "WorkspacePathError",
    "analyze_course",
    "analyze_evidence_bundle",
    "build_review",
    "default_capabilities",
    "plan_deck",
    "schedule_windows",
    "set_window_status",
    "verify_plan",
    "write_analysis_artifacts",
    "resolve_native_adapter",
]

"""Budget charging and cancellation around provider calls."""

from __future__ import annotations

from threading import Event

from yt2class.adapters.providers.base import ModelRequest, ModelResult, Provider, RequestCancelled
from yt2class.orchestration.budget import BudgetExceeded, RunBudget
from yt2class.orchestration.retry import RetryPolicy, call_with_retry


class BudgetedProvider(Provider):
    """Wrap a provider to enforce run budgets and optional retry."""

    def __init__(
        self,
        inner: Provider,
        budget: RunBudget,
        *,
        cancel_event: Event | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        super().__init__(inner.capabilities)
        self._inner = inner
        self._budget = budget
        self._cancel_event = cancel_event
        self._retry_policy = retry_policy

    def complete(
        self,
        request: ModelRequest,
        *,
        cancel_event: Event | None = None,
    ) -> ModelResult:
        active_cancel = cancel_event or self._cancel_event

        def attempt() -> ModelResult:
            if active_cancel is not None and active_cancel.is_set():
                raise RequestCancelled(f"request {request.request_id} cancelled")
            result = self._inner.complete(request, cancel_event=active_cancel)
            usage = result.usage
            if usage is None:
                raise BudgetExceeded("provider result missing usage", kind="missing_usage")
            self._budget.charge(
                input_tokens=int(usage.input_tokens),
                output_tokens=int(usage.output_tokens),
                image_count=int(request.image_count),
                video_seconds=float(request.video_seconds),
                model_calls=1,
            )
            return result

        if self._retry_policy is None:
            return attempt()
        return call_with_retry(attempt, policy=self._retry_policy)

    def _complete(
        self,
        request: ModelRequest,
        *,
        cancel_event: Event | None = None,
    ) -> ModelResult:
        return self.complete(request, cancel_event=cancel_event)


def wrap_provider(
    provider: Provider,
    budget: RunBudget,
    *,
    cancel_event: Event | None = None,
    retry_policy: RetryPolicy | None = None,
) -> Provider:
    if isinstance(provider, BudgetedProvider):
        return provider
    return BudgetedProvider(provider, budget, cancel_event=cancel_event, retry_policy=retry_policy)


__all__ = ["BudgetedProvider", "BudgetExceeded", "wrap_provider"]

from __future__ import annotations

from yt2class.domain.knowledge import KnowledgeClaim
from yt2class.stages.verify_claims import check_grounding_batch
from tests.helpers.m3 import grounding_provider


def test_grounding_batch_uses_one_provider_call_for_many_claims():
    claims = [
        KnowledgeClaim(
            id=f"claim-{index}",
            text=f"statement {index}",
            evidence_ids=["cap-1"],
            status="insufficient",
        )
        for index in range(5)
    ]
    provider = grounding_provider()
    index = {"cap-1": "shared evidence text"}
    results = check_grounding_batch(claims, index, provider=provider)
    assert len(results) == 5
    assert all(item.passed for item in results.values())
    verifier_calls = [item for item in provider.requests if item.role == "verifier"]
    assert len(verifier_calls) == 1

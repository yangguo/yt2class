"""Deterministic, reference-level coverage diagnostics for editorial plans.

This report compares KnowledgeDocument claims with EditorialPlan references. A
page reference is not evidence that the claim's meaning is visible in the copy.
"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any

from yt2class.domain.editorial import EditorialPlan
from yt2class.domain.knowledge import KnowledgeDocument, KnowledgeUnit
from yt2class.domain.resolvers import evidence_universe
from yt2class.domain.transcript import TranscriptDocument
from yt2class.domain.verification import VerificationReport
from yt2class.domain.visual import VisualCatalogue


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _model_value(value: Any | None) -> dict[str, Any] | None:
    return None if value is None else value.model_dump(mode="json")


def _issue(code: str, **details: Any) -> dict[str, Any]:
    return {"code": code, **details}


def _source_issues(
    knowledge: KnowledgeDocument,
    plan: EditorialPlan,
    transcript: TranscriptDocument | None,
    visual: VisualCatalogue | None,
    verification: VerificationReport | None,
) -> list[dict[str, Any]]:
    expected = knowledge.source_id
    issues: list[dict[str, Any]] = []
    documents = {
        "editorial_plan": plan.source_id,
        "transcript": transcript.source_id if transcript is not None else None,
        "visual_catalogue": visual.source_id if visual is not None else None,
        "verification_report": verification.source_id if verification is not None else None,
    }
    for document, source_id in documents.items():
        if source_id is not None and source_id != expected:
            issues.append(
                _issue(
                    "source_id_mismatch",
                    document=document,
                    expected_source_id=expected,
                    actual_source_id=source_id,
                )
            )
    return issues


def _verification_usable(
    knowledge: KnowledgeDocument,
    verification: VerificationReport | None,
) -> bool:
    return verification is not None and verification.source_id == knowledge.source_id


def _evidence_ids(
    knowledge: KnowledgeDocument,
    transcript: TranscriptDocument | None,
    visual: VisualCatalogue | None,
) -> set[str] | None:
    if transcript is None or visual is None:
        return None
    if transcript.source_id != knowledge.source_id or visual.source_id != knowledge.source_id:
        return None
    return evidence_universe(transcript, visual)


def _applicable_omissions(
    unit: KnowledgeUnit,
    claim_id: str,
    plan: EditorialPlan,
) -> list[dict[str, Any]]:
    def is_summary_capacity_note(reason: str) -> bool:
        return reason.startswith(
            (
                "summary coverage exceeds page budget",
                "no source-backed summary content available",
            )
        )

    return [
        {"reason": omission.reason, "topic_id": omission.topic_id, "claim_id": omission.claim_id}
        for omission in plan.omissions
        if omission.claim_id == claim_id
        or (
            omission.claim_id is None
            and omission.topic_id == unit.topic_id
            and not is_summary_capacity_note(omission.reason)
        )
    ]


def build_content_coverage_report(
    knowledge: KnowledgeDocument,
    plan: EditorialPlan,
    *,
    transcript: TranscriptDocument | None = None,
    visual: VisualCatalogue | None = None,
    verification: VerificationReport | None = None,
) -> dict[str, Any]:
    """Return reference-level coverage and cross-document diagnostics.

    ``content`` page references are reported as body references, ``summary``
    references are reported separately, and cover/quiz references are exposed
    without treating them as lesson-body coverage. Evidence is resolvable only
    when both supplied catalogues belong to the knowledge document's source.
    """

    claims_by_id: dict[str, tuple[KnowledgeUnit, Any]] = {
        claim.id: (unit, claim)
        for unit in knowledge.units
        for claim in unit.claims
    }
    evidence_ids = _evidence_ids(knowledge, transcript, visual)
    evidence_validation_status = "checked" if evidence_ids is not None else "not_checked"
    evidence_validation_reason: str | None = None
    if evidence_ids is None:
        if transcript is None or visual is None:
            evidence_validation_reason = "transcript_and_visual_catalogues_required"
        else:
            evidence_validation_reason = "source_id_mismatch"

    issues = _source_issues(knowledge, plan, transcript, visual, verification)
    known_topics = {unit.topic_id for unit in knowledge.units}
    body_pages: dict[str, list[str]] = {claim_id: [] for claim_id in claims_by_id}
    summary_pages: dict[str, list[str]] = {claim_id: [] for claim_id in claims_by_id}
    other_pages: dict[str, list[str]] = {claim_id: [] for claim_id in claims_by_id}
    unknown_references: list[str] = []

    for page in plan.pages:
        for claim_id in page.claim_ids:
            if claim_id not in claims_by_id:
                unknown_references.append(claim_id)
                issues.append(
                    _issue(
                        "unknown_claim_reference",
                        origin="editorial_page",
                        page_id=page.id,
                        page_type=page.type,
                        claim_id=claim_id,
                    )
                )
                continue
            target = (
                body_pages[claim_id]
                if page.type == "content"
                else summary_pages[claim_id]
                if page.type == "summary"
                else other_pages[claim_id]
            )
            target.append(page.id)

    for omission in plan.omissions:
        if omission.topic_id is not None and omission.topic_id not in known_topics:
            issues.append(
                _issue(
                    "unknown_topic_reference",
                    origin="editorial_omission",
                    topic_id=omission.topic_id,
                    claim_id=omission.claim_id,
                )
            )
        if omission.claim_id is not None and omission.claim_id not in claims_by_id:
            issues.append(
                _issue(
                    "unknown_claim_reference",
                    origin="editorial_omission",
                    topic_id=omission.topic_id,
                    claim_id=omission.claim_id,
                    reason=omission.reason,
                )
            )
        if (
            omission.claim_id is not None
            and omission.claim_id in claims_by_id
            and omission.topic_id is not None
            and claims_by_id[omission.claim_id][0].topic_id != omission.topic_id
        ):
            issues.append(
                _issue(
                    "omission_topic_mismatch",
                    topic_id=omission.topic_id,
                    claim_id=omission.claim_id,
                    actual_topic_id=claims_by_id[omission.claim_id][0].topic_id,
                )
            )

    usable_verification = (
        verification if _verification_usable(knowledge, verification) else None
    )
    verdicts = (
        {item.claim_id: item for item in usable_verification.verdicts}
        if usable_verification is not None
        else {}
    )
    if verification is not None and verification.source_id == knowledge.source_id:
        for item in verification.verdicts:
            if item.claim_id not in claims_by_id:
                issues.append(
                    _issue(
                        "unknown_verification_claim",
                        claim_id=item.claim_id,
                    )
                )
            if evidence_ids is not None:
                missing = sorted(
                    (set(item.supporting_ids) | set(item.contradicting_ids)) - evidence_ids
                )
                for evidence_id in missing:
                    issues.append(
                        _issue(
                            "invalid_verification_evidence_reference",
                            claim_id=item.claim_id,
                            evidence_id=evidence_id,
                        )
                    )

    report_claims: list[dict[str, Any]] = []
    unaccounted_ids: list[str] = []
    for unit in knowledge.units:
        for claim in unit.claims:
            body_ids = body_pages[claim.id]
            summary_ids = summary_pages[claim.id]
            other_ids = other_pages[claim.id]
            omission_refs = _applicable_omissions(unit, claim.id, plan)
            omission_reasons = list(dict.fromkeys(item["reason"] for item in omission_refs))
            referenced = bool(body_ids or summary_ids or other_ids)
            if body_ids:
                coverage_status = "body-reference"
            elif summary_ids:
                coverage_status = "summary-only"
            elif omission_refs:
                coverage_status = "explicitly-omitted"
            elif other_ids:
                coverage_status = "other-reference"
            else:
                coverage_status = "unaccounted"
                unaccounted_ids.append(claim.id)
                issues.append(
                    _issue(
                        "claim_unaccounted",
                        topic_id=unit.topic_id,
                        unit_id=unit.id,
                        claim_id=claim.id,
                        other_page_ids=other_ids,
                    )
                )

            if referenced and omission_refs:
                issues.append(
                    _issue(
                        "claim_both_referenced_and_omitted",
                        topic_id=unit.topic_id,
                        unit_id=unit.id,
                        claim_id=claim.id,
                        body_page_ids=body_ids,
                        summary_page_ids=summary_ids,
                        other_page_ids=other_ids,
                        omission_reasons=omission_reasons,
                    )
                )

            invalid_evidence_ids = (
                [item for item in claim.evidence_ids if item not in evidence_ids]
                if evidence_ids is not None
                else []
            )
            for evidence_id in invalid_evidence_ids:
                issues.append(
                    _issue(
                        "invalid_evidence_reference",
                        topic_id=unit.topic_id,
                        unit_id=unit.id,
                        claim_id=claim.id,
                        evidence_id=evidence_id,
                    )
                )

            verdict = verdicts.get(claim.id)
            report_claims.append(
                {
                    "topic_id": unit.topic_id,
                    "unit_id": unit.id,
                    "claim_id": claim.id,
                    "knowledge_status": claim.status,
                    "provenance": claim.provenance,
                    "verification_status": (
                        verdict.verdict
                        if verdict is not None
                        else "not_checked"
                        if verification is not None and not _verification_usable(knowledge, verification)
                        else "not_available"
                        if verification is None
                        else "not_reported"
                    ),
                    "verification_reason": verdict.reason if verdict is not None else None,
                    "verification_supporting_evidence_ids": (
                        list(verdict.supporting_ids) if verdict is not None else []
                    ),
                    "verification_contradicting_evidence_ids": (
                        list(verdict.contradicting_ids) if verdict is not None else []
                    ),
                    "coverage_status": coverage_status,
                    "body_page_ids": body_ids,
                    "summary_page_ids": summary_ids,
                    "other_page_ids": other_ids,
                    "omission_reasons": omission_reasons,
                    "omissions": omission_refs,
                    "evidence_ids": list(claim.evidence_ids),
                    "invalid_evidence_ids": invalid_evidence_ids,
                    "evidence_validation_status": evidence_validation_status,
                    "evidence_reference_status": (
                        "unresolved"
                        if invalid_evidence_ids
                        else "resolved"
                        if evidence_ids is not None
                        else "not_checked"
                    ),
                }
            )

    summary_pages_exist = any(page.type == "summary" for page in plan.pages)
    summary_claim_ids = [
        claim_id for claim_id, page_ids in summary_pages.items() if page_ids
    ]
    body_topics: dict[str, list[str]] = {}
    summary_topics: dict[str, list[str]] = {}
    claim_topics = {claim_id: unit.topic_id for claim_id, (unit, _claim) in claims_by_id.items()}
    for claim_id, page_ids in body_pages.items():
        if page_ids:
            body_topics.setdefault(claim_topics[claim_id], []).append(claim_id)
    for claim_id in summary_claim_ids:
        summary_topics.setdefault(claim_topics[claim_id], []).append(claim_id)

    topic_coverage: list[dict[str, Any]] = []
    if summary_pages_exist:
        for topic_id, body_claim_ids in body_topics.items():
            topic_summary_claim_ids = summary_topics.get(topic_id, [])
            missing_summary = not topic_summary_claim_ids
            topic_coverage.append(
                {
                    "topic_id": topic_id,
                    "body_claim_ids": body_claim_ids,
                    "summary_claim_ids": topic_summary_claim_ids,
                    "summary_missing": missing_summary,
                }
            )
            if missing_summary:
                issues.append(
                    _issue(
                        "topic_missing_summary",
                        topic_id=topic_id,
                        body_claim_ids=body_claim_ids,
                    )
                )

    issue_counts: dict[str, int] = {}
    for item in issues:
        issue_counts[item["code"]] = issue_counts.get(item["code"], 0) + 1
    coverage_counts: dict[str, int] = {}
    for item in report_claims:
        coverage_counts[item["coverage_status"]] = coverage_counts.get(item["coverage_status"], 0) + 1

    knowledge_value = knowledge.model_dump(mode="json")
    plan_value = plan.model_dump(mode="json")
    transcript_value = _model_value(transcript)
    visual_value = _model_value(visual)
    verification_value = _model_value(verification)
    digests = {
        "knowledge_sha256": _digest(knowledge_value),
        "transcript_sha256": _digest(transcript_value) if transcript_value is not None else None,
        "visual_catalogue_sha256": _digest(visual_value) if visual_value is not None else None,
        "editorial_plan_sha256": _digest(plan_value),
        "verification_report_sha256": (
            _digest(verification_value) if verification_value is not None else None
        ),
    }
    digests["source_inputs_sha256"] = _digest(
        {
            "knowledge_sha256": digests["knowledge_sha256"],
            "transcript_sha256": digests["transcript_sha256"],
            "visual_catalogue_sha256": digests["visual_catalogue_sha256"],
        }
    )

    return {
        "schema_version": "1.0",
        "report_type": "content-coverage-diagnostic",
        "source_id": knowledge.source_id,
        "coverage_semantics": (
            "Page claim_ids are references only. This report does not establish semantic visibility, "
            "page-copy grounding, or claim verification merely because a claim is referenced."
        ),
        "digests": digests,
        "evidence_validation": {
            "status": evidence_validation_status,
            "reason": evidence_validation_reason,
            "known_evidence_id_count": len(evidence_ids) if evidence_ids is not None else None,
            "semantics": (
                "Resolved means the ID exists in the supplied source-matched catalogues; it does not establish support."
            ),
        },
        "summary_exists": summary_pages_exist,
        "summary": {
            "claim_count": len(report_claims),
            "coverage_counts": coverage_counts,
            "issue_count": len(issues),
            "issue_counts_by_code": issue_counts,
            "unaccounted_claim_ids": unaccounted_ids,
            "topics_missing_summary": [
                item["topic_id"] for item in topic_coverage if item["summary_missing"]
            ],
            "unknown_claim_references": list(dict.fromkeys(unknown_references)),
        },
        "topic_coverage": topic_coverage,
        "claims": report_claims,
        "issues": issues,
    }


__all__ = ["build_content_coverage_report"]

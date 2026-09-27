from __future__ import annotations

from yt2class.domain.editorial import EditorialPlan, Omission, PageIntent
from yt2class.domain.knowledge import KnowledgeClaim, KnowledgeDocument, KnowledgeUnit
from yt2class.domain.verification import ClaimVerdict, VerificationReport
from yt2class.stages.content_coverage import build_content_coverage_report
from tests.helpers.m3 import lecture_knowledge


def _claim(claim_id: str, *, evidence_id: str = "cap-001", status: str = "draft", provenance: str = "source"):
    return KnowledgeClaim(
        id=claim_id,
        text=f"Claim {claim_id}",
        evidence_ids=[evidence_id],
        status=status,
        provenance=provenance,
    )


def _knowledge() -> KnowledgeDocument:
    return KnowledgeDocument(
        schema_version="1.0",
        source_id="src-demo",
        units=[
            KnowledgeUnit(
                id="unit-alpha",
                topic_id="topic-alpha",
                segment_ids=["window-alpha"],
                start_seconds=0.0,
                end_seconds=30.0,
                kind="concept",
                claims=[
                    _claim("claim-alpha-body"),
                    _claim("claim-alpha-summary", status="supported"),
                    _claim("claim-alpha-practice", provenance="generated-practice"),
                    _claim("claim-alpha-free"),
                ],
            ),
            KnowledgeUnit(
                id="unit-beta",
                topic_id="topic-beta",
                segment_ids=["window-beta"],
                start_seconds=30.0,
                end_seconds=60.0,
                kind="example",
                claims=[
                    _claim("claim-beta-body", evidence_id="frame-001"),
                    _claim("claim-beta-omitted", evidence_id="frame-001"),
                    _claim("claim-invalid-evidence", evidence_id="ev-not-in-input"),
                ],
            ),
        ],
    )


def _plan() -> EditorialPlan:
    return EditorialPlan(
        schema_version="1.0",
        source_id="src-demo",
        target_pages=4,
        max_pages=4,
        pages=[
            PageIntent(
                id="page-alpha-body",
                type="content",
                title="Alpha",
                claim_ids=["claim-alpha-body", "unknown-claim"],
                selection_reason="fixture",
            ),
            PageIntent(
                id="page-beta-body",
                type="content",
                title="Beta",
                claim_ids=["claim-beta-body"],
                selection_reason="fixture",
            ),
            PageIntent(
                id="page-summary",
                type="summary",
                title="Summary",
                claim_ids=["claim-alpha-body", "claim-alpha-summary"],
                selection_reason="fixture",
            ),
            PageIntent(
                id="page-quiz",
                type="quiz",
                title="Quiz",
                claim_ids=["claim-alpha-practice", "claim-alpha-free"],
                selection_reason="fixture",
            ),
        ],
        omissions=[
            Omission(claim_id="claim-alpha-body", reason="also listed as omitted"),
            Omission(claim_id="claim-beta-omitted", reason="outside the lesson scope"),
            Omission(claim_id="unknown-omitted", reason="stale editor reference"),
        ],
    )


def test_report_tracks_reference_omission_topic_and_evidence_gaps_without_claiming_visibility():
    _source_knowledge, _topics, transcript, visual = lecture_knowledge()
    verification = VerificationReport(
        schema_version="1.0",
        source_id="src-demo",
        quality_mode="draft",
        verdicts=[
            ClaimVerdict(
                claim_id="claim-alpha-body",
                verdict="supported",
                supporting_ids=["cap-001"],
                reason="fixture verifier result",
            )
        ],
    )

    report = build_content_coverage_report(
        _knowledge(),
        _plan(),
        transcript=transcript,
        visual=visual,
        verification=verification,
    )

    claims = {item["claim_id"]: item for item in report["claims"]}
    assert claims["claim-alpha-body"]["coverage_status"] == "body-reference"
    assert claims["claim-alpha-body"]["body_page_ids"] == ["page-alpha-body"]
    assert claims["claim-alpha-body"]["summary_page_ids"] == ["page-summary"]
    assert claims["claim-alpha-body"]["omission_reasons"] == ["also listed as omitted"]
    assert claims["claim-alpha-body"]["knowledge_status"] == "draft"
    assert claims["claim-alpha-body"]["verification_status"] == "supported"
    assert claims["claim-alpha-summary"]["coverage_status"] == "summary-only"
    assert claims["claim-beta-omitted"]["coverage_status"] == "explicitly-omitted"
    assert claims["claim-alpha-free"]["coverage_status"] == "other-reference"
    assert claims["claim-alpha-practice"]["coverage_status"] == "other-reference"
    assert claims["claim-alpha-practice"]["other_page_ids"] == ["page-quiz"]
    assert claims["claim-invalid-evidence"]["invalid_evidence_ids"] == ["ev-not-in-input"]
    assert claims["claim-invalid-evidence"]["evidence_validation_status"] == "checked"

    issue_codes = [item["code"] for item in report["issues"]]
    assert "unknown_claim_reference" in issue_codes
    assert "claim_both_referenced_and_omitted" in issue_codes
    assert "claim_unaccounted" in issue_codes
    assert "topic_missing_summary" in issue_codes
    assert "invalid_evidence_reference" in issue_codes
    assert report["evidence_validation"]["status"] == "checked"
    assert "does not establish semantic visibility" in report["coverage_semantics"]


def test_report_marks_evidence_unchecked_when_catalogues_are_not_supplied():
    report = build_content_coverage_report(_knowledge(), _plan())

    assert report["evidence_validation"]["status"] == "not_checked"
    assert all(
        claim["evidence_validation_status"] == "not_checked"
        for claim in report["claims"]
    )
    assert all(not claim["invalid_evidence_ids"] for claim in report["claims"])


def test_report_does_not_resolve_matching_ids_from_another_source():
    _source_knowledge, _topics, transcript, visual = lecture_knowledge()
    other_transcript = transcript.model_copy(update={"source_id": "src-other"})
    other_verification = VerificationReport(
        schema_version="1.0",
        source_id="src-other",
        quality_mode="draft",
        verdicts=[
            ClaimVerdict(
                claim_id="claim-alpha-body",
                verdict="supported",
                supporting_ids=["cap-001"],
                reason="belongs to a different source",
            )
        ],
    )

    report = build_content_coverage_report(
        _knowledge(),
        _plan(),
        transcript=other_transcript,
        visual=visual,
        verification=other_verification,
    )

    claims = {item["claim_id"]: item for item in report["claims"]}
    assert report["evidence_validation"]["status"] == "not_checked"
    assert claims["claim-alpha-body"]["evidence_validation_status"] == "not_checked"
    assert claims["claim-alpha-body"]["verification_status"] == "not_checked"
    assert claims["claim-alpha-body"]["invalid_evidence_ids"] == []
    assert any(item["code"] == "source_id_mismatch" for item in report["issues"])


def test_report_digests_follow_the_knowledge_and_final_plan():
    knowledge = _knowledge()
    plan = _plan()

    first = build_content_coverage_report(knowledge, plan)
    same = build_content_coverage_report(knowledge, plan)
    changed_plan = build_content_coverage_report(
        knowledge,
        plan.model_copy(update={"pages": [plan.pages[0].model_copy(update={"title": "Edited"}), *plan.pages[1:]]}),
    )

    assert first["digests"] == same["digests"]
    assert first["digests"]["knowledge_sha256"]
    assert first["digests"]["editorial_plan_sha256"] != changed_plan["digests"]["editorial_plan_sha256"]


def test_summary_capacity_notes_do_not_mark_each_topic_claim_as_omitted():
    base_plan = _plan()
    plan = base_plan.model_copy(
        update={
            "omissions": [
                *base_plan.omissions,
                Omission(
                    topic_id="topic-beta",
                    reason="summary coverage exceeds page budget; review required",
                ),
            ]
        }
    )

    report = build_content_coverage_report(_knowledge(), plan)
    beta_body = next(item for item in report["claims"] if item["claim_id"] == "claim-beta-body")

    assert beta_body["coverage_status"] == "body-reference"
    assert beta_body["omission_reasons"] == []
    assert not any(
        issue["code"] == "claim_both_referenced_and_omitted"
        and issue["claim_id"] == "claim-beta-body"
        for issue in report["issues"]
    )

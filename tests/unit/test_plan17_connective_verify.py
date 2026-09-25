"""接续 survives verify when its gloss claims are soft-failed."""

from __future__ import annotations

from yt2class.adapters.providers.base import FakeProvider
from yt2class.domain.editorial import EditorialPlan, PageIntent
from yt2class.domain.verification import ClaimVerdict
from yt2class.stages.grammar_sense import CONNECTIVE_HEADING
from yt2class.stages.verify_claims import formalize_plan, verify_claims
from tests.helpers.m2 import frames_caps, make_transcript, make_visual
from tests.helpers.m3 import concept_unit, knowledge


def _plan(*pages: PageIntent) -> EditorialPlan:
    return EditorialPlan(
        schema_version="1.0",
        source_id="src-demo",
        target_pages=4,
        max_pages=8,
        pages=list(pages),
    )


def _page(
    page_id: str,
    title: str,
    claim_ids: list[str],
    *,
    page_type: str = "content",
    reason: str = "候选",
    body: list[str] | None = None,
) -> PageIntent:
    return PageIntent(
        id=page_id,
        type=page_type,
        title=title,
        claim_ids=claim_ids,
        notes="",
        selection_reason=reason,
        quality_label="draft",
        body_points=body or [],
    )


def _tsuki_doc():
    return knowledge(
        concept_unit(
            "unit-conn",
            "claim-connective",
            "接续：名詞／数量詞＋につき",
            ["cap-conn"],
            topic_id="topic-conn",
            start=0.0,
            end=12.0,
        ),
        concept_unit(
            "unit-cause",
            "claim-cause",
            "工事中につき、通路を変更しています。",
            ["cap-cause"],
            topic_id="topic-cause",
            start=12.0,
            end=24.0,
            kind="example",
        ),
        concept_unit(
            "unit-junk",
            "claim-junk",
            "这是闲聊，没有用法。",
            ["cap-junk"],
            topic_id="topic-junk",
            start=24.0,
            end=36.0,
        ),
    )


def _incoming_plan() -> EditorialPlan:
    return _plan(
        _page("cover", "～につき", [], page_type="cover", reason="封面"),
        _page(
            "page-conn",
            CONNECTIVE_HEADING,
            ["claim-connective"],
            reason="mandatory-connective lock",
            body=["名詞／数量詞＋につき"],
        ),
        _page(
            "page-cause",
            "用法一：原因・理由",
            ["claim-cause"],
            reason="mandatory-sense lock",
            body=["工事中につき、通路を変更しています。"],
        ),
        _page("page-junk", "闲聊", ["claim-junk"], body=["这是闲聊，没有用法。"]),
        _page(
            "summary",
            "本课总结",
            ["claim-cause"],
            page_type="summary",
            reason="总结",
            body=["用法一：原因・理由"],
        ),
    )


def test_formalize_keeps_connective_page_when_claims_are_removed():
    doc = _tsuki_doc()
    plan = _incoming_plan()
    verdicts = [
        ClaimVerdict(
            claim_id="claim-connective",
            verdict="insufficient",
            reason="gloss not entailed",
        ),
        ClaimVerdict(
            claim_id="claim-cause",
            verdict="supported",
            supporting_ids=["cap-cause"],
            reason="evidence-grounded support",
        ),
        ClaimVerdict(
            claim_id="claim-junk",
            verdict="insufficient",
            reason="chatter",
        ),
    ]
    formal = formalize_plan(
        plan,
        verdicts=verdicts,
        knowledge=doc,
        quality_mode="draft",
        removed=["claim-connective", "claim-junk"],
    )
    titles = [page.title for page in formal.pages]
    assert any(title.startswith("接续") for title in titles)
    connective = next(page for page in formal.pages if page.title.startswith("接续"))
    assert connective.claim_ids == ["claim-connective"]
    assert connective.body_points == ["名詞／数量詞＋につき"]
    assert connective.quality_label == "draft"
    assert "page-junk" not in {page.id for page in formal.pages}
    assert any(page.id == "page-cause" for page in formal.pages)
    cause = next(page for page in formal.pages if page.id == "page-cause")
    assert cause.claim_ids == ["claim-cause"]


def test_formalize_still_drops_unmarked_pages_and_non_tsuki_connective_titles():
    doc = knowledge(
        concept_unit(
            "unit-other",
            "claim-other",
            "加热三分钟。",
            ["cap-other"],
            start=0.0,
            end=10.0,
        )
    )
    plan = _plan(
        _page("cover", "课程", [], page_type="cover", reason="封面"),
        _page("page-link", "接续说明", ["claim-other"], body=["先看定义。"]),
        _page(
            "summary",
            "总结",
            ["claim-other"],
            page_type="summary",
            reason="总结",
            body=["总结"],
        ),
        _page("pad", "补充", [], reason="占位"),
    )
    formal = formalize_plan(
        plan,
        verdicts=[
            ClaimVerdict(claim_id="claim-other", verdict="insufficient", reason="ungrounded"),
        ],
        knowledge=doc,
        quality_mode="draft",
        removed=["claim-other"],
    )
    assert all(not page.title.startswith("接续") for page in formal.pages)


def _scripted_provider():
    def respond(provider, _request):
        payload = provider.last_payload or {}
        claims = payload.get("claims") or []
        if not claims:
            return {}
        evidence = payload.get("evidence") or {}
        rows = []
        for claim in claims:
            claim_id = str(claim.get("id") or "")
            cited = [
                item
                for item in (claim.get("evidence_ids") or [])
                if str(evidence.get(item, "")).strip()
            ]
            if claim_id in {"claim-connective", "claim-junk"}:
                rows.append(
                    dict(
                        claim_id=claim_id,
                        verdict="insufficient",
                        supporting_ids=[],
                        contradicting_ids=[],
                        reason="soft-failed gloss",
                    )
                )
            else:
                rows.append(
                    dict(
                        claim_id=claim_id,
                        verdict="supported",
                        supporting_ids=cited[:4] or ["cap-cause"],
                        contradicting_ids=[],
                        reason="scripted support",
                    )
                )
        return {"verdicts": rows}

    return FakeProvider(frames_caps(), responder=respond)


def test_verify_claims_restores_connective_page_after_soft_fail():
    doc = _tsuki_doc()
    transcript = make_transcript(
        [
            ("cap-conn", 0.0, 12.0, "名詞と数量詞の後につきを付けます。"),
            ("cap-cause", 12.0, 24.0, "工事中につき、通路を変更しています。"),
            ("cap-junk", 24.0, 36.0, "これは雑談です。"),
        ],
        duration=40.0,
        language="ja",
    )
    visual = make_visual([], duration=40.0)
    outcome = verify_claims(
        doc,
        plan=_incoming_plan(),
        transcript=transcript,
        visual=visual,
        provider=_scripted_provider(),
        quality_mode="draft",
    )
    assert "claim-connective" in outcome.report.removed_from_formal
    titles = [page.title for page in outcome.plan.pages]
    connective = next(page for page in outcome.plan.pages if page.title.startswith("接续"))
    assert connective.claim_ids == ["claim-connective"]
    assert "名詞／数量詞＋につき" in " ".join(connective.body_points)
    assert CONNECTIVE_HEADING in titles
    assert "page-junk" not in {page.id for page in outcome.plan.pages}
    assert any(page.id == "page-cause" and page.claim_ids == ["claim-cause"] for page in outcome.plan.pages)

"""Claim verifier: structured fact checks, one repair, then formalize the deck."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from threading import Event
from typing import Any, Iterable

from yt2class.adapters.providers.base import Provider, RequestCancelled
from yt2class.orchestration.budget import BudgetExceeded
from yt2class.orchestration.concurrency import map_parallel
from yt2class.domain.editorial import (
    QUALITY_NOTE_MARKERS,
    EditorialPlan,
    Omission,
    PageIntent,
    relabel_page,
)
from yt2class.domain.knowledge import KnowledgeClaim, KnowledgeDocument, KnowledgeUnit
from yt2class.domain.transcript import TranscriptDocument
from yt2class.domain.verification import (
    CheckKind,
    ClaimVerdict,
    HumanSample,
    QualityMode,
    Verdict,
    VerificationReport,
    strict_closure_errors,
)
from yt2class.domain.visual import VisualCatalogue
from yt2class.stages.grammar_sense import is_tsuki_grammar_lesson
from yt2class.stages.llm_util import (
    allowed_evidence_ids,
    attach_provider_payload,
    load_prompt,
    model_request,
)

NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
_PEDAGOGICAL_NUMBER_RE = re.compile(
    r"(?:第\s*\d+|\d+\s*例|用法\s*\d+|例\s*\d+)",
    re.IGNORECASE,
)
CRITICAL_KINDS = {
    "number",
    "negation",
    "condition",
    "proper_name",
    "translation",
    "step",
    "image_text",
    "contradiction",
    "unknown_ref",
    "grounding",
}
QUALITY_PREFIXES = ("[DRAFT]", "[EVIDENCE-ONLY]")
VERIFIER_GROUNDING_BATCH_SIZE = 15


class StrictVerificationError(ValueError):
    """strict mode cannot emit verified labels while critical claims are unresolved."""

    def __init__(self, message: str, outcome: VerifyOutcome | None = None) -> None:
        super().__init__(message)
        self.outcome = outcome


@dataclass
class CheckResult:
    kind: CheckKind
    passed: bool
    note: str
    verdict: Verdict | None = None
    supporting_ids: list[str] = field(default_factory=list)
    contradicting_ids: list[str] = field(default_factory=list)


@dataclass
class VerifyOutcome:
    report: VerificationReport
    knowledge: KnowledgeDocument
    plan: EditorialPlan
    repaired: bool = False
    verified_claim_ids: list[str] = field(default_factory=list)
    error: str | None = None

    def m3_gate_ok(self) -> bool:
        if self.report.quality_mode != "strict":
            return False
        claim_ids = {claim.id for claim in self.knowledge.iter_claims()}
        return not strict_closure_errors(self.report, claim_ids=claim_ids)


def evidence_index(
    transcript: TranscriptDocument,
    visual: VisualCatalogue,
) -> dict[str, str]:
    texts: dict[str, str] = {}
    for segment in transcript.segments:
        texts[segment.id] = segment.text_original
    for region in visual.ocr_regions:
        texts[region.id] = region.text
        texts[region.parent_occurrence_id] = (
            texts.get(region.parent_occurrence_id, "") + " " + region.text
        ).strip()
    return texts


def _join(ids: Iterable[str], index: dict[str, str]) -> str:
    return " ".join(index.get(item, "") for item in ids if index.get(item))


def _frame_time(frame_id: str, visual: VisualCatalogue) -> float | None:
    for occurrence in visual.occurrences:
        if occurrence.id != frame_id:
            continue
        if occurrence.actual_source_seconds is not None:
            return float(occurrence.actual_source_seconds)
        if occurrence.timestamp_seconds is not None:
            return float(occurrence.timestamp_seconds)
        return float(occurrence.requested_seconds)
    return None


def _segment_time(segment_id: str, transcript: TranscriptDocument) -> float | None:
    for segment in transcript.segments:
        if segment.id == segment_id:
            return float(segment.start_seconds)
    return None


def _claim_times(
    claim: KnowledgeClaim,
    transcript: TranscriptDocument,
    visual: VisualCatalogue,
) -> list[float]:
    times: list[float] = []
    for item in claim.evidence_ids:
        stamp = _frame_time(item, visual)
        if stamp is None:
            stamp = _segment_time(item, transcript)
        if stamp is not None:
            times.append(stamp)
    return times


def copy_is_affirmed(
    text: str,
    evidence_text: str,
    *,
    practice: bool = False,
    provider: Provider | None = None,
) -> bool:
    """Require provider affirmation for every nonempty piece of page copy."""
    if not text.strip():
        return True
    claim = KnowledgeClaim(
        id="copy", status="insufficient", text=text, evidence_ids=["excerpt"],
        provenance="generated-practice" if practice else "source",
    )
    return (
        check_numbers(claim, evidence_text).passed
        and check_grounding(claim, {"excerpt": evidence_text}, provider=provider).passed
    )


def notes_without_quality(notes: str) -> str:
    cleaned = notes
    for marker in QUALITY_NOTE_MARKERS.values():
        if cleaned.startswith(marker):
            cleaned = cleaned[len(marker) :].strip()
            break
    else:
        for prefix in QUALITY_PREFIXES:
            if cleaned.startswith(prefix):
                cleaned = cleaned[len(prefix) :].strip()
                break
    return cleaned


def page_evidence_text(
    page: PageIntent,
    *,
    knowledge: KnowledgeDocument,
    transcript: TranscriptDocument,
    visual: VisualCatalogue,
    index: dict[str, str] | None = None,
) -> str:
    if index is None:
        index = evidence_index(transcript, visual)
    evidence_ids: list[str] = []
    claim_ids = set(page.claim_ids)
    for unit in knowledge.units:
        for claim in unit.claims:
            if claim.id in claim_ids:
                evidence_ids.extend(claim.evidence_ids)
    return _join(dict.fromkeys(evidence_ids), index)


def check_unknown_refs(claim: KnowledgeClaim, allowed: set[str]) -> CheckResult:
    missing = [item for item in claim.evidence_ids if item not in allowed]
    if missing:
        return CheckResult(
            kind="unknown_ref",
            passed=False,
            note=f"unknown evidence refs {missing}",
            verdict="insufficient",
        )
    return CheckResult(kind="unknown_ref", passed=True, note="refs resolve")


def _grounding_failure(note: str = "no affirmative provider evidence support") -> CheckResult:
    return CheckResult(
        kind="grounding",
        passed=False,
        note=note,
        verdict="insufficient",
    )


def _grounding_from_row(
    claim: KnowledgeClaim,
    index: dict[str, str],
    row: dict[str, Any],
    *,
    evidence_ids: list[str] | None = None,
) -> CheckResult:
    failure = _grounding_failure()
    cited_ids = list(evidence_ids or claim.evidence_ids)
    try:
        result = ClaimVerdict.model_validate(row)
    except (ValueError, TypeError):
        return failure
    refs = result.supporting_ids + result.contradicting_ids
    if result.claim_id != claim.id or any(
        item not in cited_ids or not index.get(item, "").strip() for item in refs
    ):
        return failure
    if result.verdict == "supported" and not result.supporting_ids:
        return failure
    if result.verdict == "contradicted" and not result.contradicting_ids:
        return failure
    return CheckResult(
        kind="grounding",
        passed=result.verdict == "supported",
        verdict=result.verdict,
        note=result.reason,
        supporting_ids=result.supporting_ids,
        contradicting_ids=result.contradicting_ids,
    )


def check_grounding_batch(
    claims: list[KnowledgeClaim],
    index: dict[str, str],
    *,
    provider: Provider | None = None,
    cancel_event: Event | None = None,
    evidence_ids_by_claim: dict[str, list[str]] | None = None,
) -> dict[str, CheckResult]:
    """Batch grounding checks using the verifier contract's multi-claim payload."""

    results: dict[str, CheckResult] = {}
    eligible: list[KnowledgeClaim] = []
    for claim in claims:
        cited_ids = list((evidence_ids_by_claim or {}).get(claim.id, claim.evidence_ids))
        if not claim.text.strip() or not cited_ids or not any(
            index.get(item, "").strip() for item in cited_ids
        ):
            results[claim.id] = _grounding_failure("missing or empty cited evidence")
        else:
            eligible.append(claim)
    if provider is None:
        for claim in eligible:
            results[claim.id] = _grounding_failure()
        return results

    for offset in range(0, len(eligible), VERIFIER_GROUNDING_BATCH_SIZE):
        chunk = eligible[offset : offset + VERIFIER_GROUNDING_BATCH_SIZE]
        cited_by_id = {
            claim.id: list((evidence_ids_by_claim or {}).get(claim.id, claim.evidence_ids))
            for claim in chunk
        }
        evidence_ids = sorted({item for ids in cited_by_id.values() for item in ids})
        payload = {
            "prompt": load_prompt("verifier.md"),
            "claims": [
                claim.model_copy(update={"evidence_ids": cited_by_id[claim.id]}).model_dump(
                    mode="json"
                )
                for claim in chunk
            ],
            "evidence": {item: index.get(item, "") for item in evidence_ids},
            "allowed_evidence_ids": evidence_ids,
        }
        structured = _complete_verifier(
            provider,
            payload,
            request_id=f"verifier:ground:batch:{offset}",
            cancel_event=cancel_event,
        )
        rows = structured.get("verdicts") if structured else None
        if not isinstance(rows, list) or len(rows) != len(chunk):
            for claim in chunk:
                results[claim.id] = _check_grounding_single(
                    claim,
                    index,
                    provider=provider,
                    cancel_event=cancel_event,
                    evidence_ids=cited_by_id[claim.id],
                )
            continue
        by_id = {str(row.get("claim_id")): row for row in rows if isinstance(row, dict)}
        for claim in chunk:
            row = by_id.get(claim.id)
            if row is None:
                results[claim.id] = _grounding_failure()
            else:
                results[claim.id] = _grounding_from_row(
                    claim,
                    index,
                    row,
                    evidence_ids=cited_by_id[claim.id],
                )
    return results


def _check_grounding_single(
    claim: KnowledgeClaim,
    index: dict[str, str],
    *,
    provider: Provider | None = None,
    cancel_event: Event | None = None,
    evidence_ids: list[str] | None = None,
) -> CheckResult:
    """One claim, one verifier request (batch fallback and public single-check API)."""
    cited_ids = list(evidence_ids or claim.evidence_ids)
    if not claim.text.strip() or not cited_ids or not any(
        index.get(item, "").strip() for item in cited_ids
    ):
        return _grounding_failure("missing or empty cited evidence")
    if provider is None:
        return _grounding_failure()
    payload = {
        "prompt": load_prompt("verifier.md"),
        "claims": [claim.model_copy(update={"evidence_ids": cited_ids}).model_dump(mode="json")],
        "evidence": {item: index.get(item, "") for item in cited_ids},
        "allowed_evidence_ids": cited_ids,
    }
    structured = _complete_verifier(
        provider,
        payload,
        request_id=f"verifier:ground:{claim.id}",
        cancel_event=cancel_event,
    )
    rows = structured.get("verdicts") if structured else None
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        return _grounding_failure()
    return _grounding_from_row(claim, index, rows[0], evidence_ids=cited_ids)


def check_grounding(
    claim: KnowledgeClaim,
    index: dict[str, str],
    *,
    provider: Provider | None = None,
    cancel_event: Event | None = None,
    evidence_ids: list[str] | None = None,
) -> CheckResult:
    """Only a structured provider verdict can affirm free-text entailment."""
    return _check_grounding_single(
        claim,
        index,
        provider=provider,
        cancel_event=cancel_event,
        evidence_ids=evidence_ids,
    )


def _pedagogical_number_literals(text: str) -> set[str]:
    literals: set[str] = set()
    for match in _PEDAGOGICAL_NUMBER_RE.finditer(text):
        for token in NUMBER_RE.findall(match.group(0)):
            literals.add(token)
    return literals


def _material_numbers(text: str) -> list[str]:
    claimed = NUMBER_RE.findall(text)
    ignore = _pedagogical_number_literals(text)
    return [item for item in claimed if item not in ignore]


def _segment_overlaps_unit(
    segment_id: str,
    unit: KnowledgeUnit,
    transcript: TranscriptDocument,
) -> bool:
    for segment in transcript.segments:
        if segment.id != segment_id:
            continue
        return (
            segment.end_seconds > unit.start_seconds
            and segment.start_seconds < unit.end_seconds
        )
    return True


def _aligned_claim_evidence_ids(
    claim: KnowledgeClaim,
    unit: KnowledgeUnit,
    transcript: TranscriptDocument,
) -> list[str]:
    segment_ids = {segment.id for segment in transcript.segments}
    aligned: list[str] = []
    for item in claim.evidence_ids:
        if item in segment_ids and not _segment_overlaps_unit(item, unit, transcript):
            continue
        aligned.append(item)
    return aligned


def check_numbers(claim: KnowledgeClaim, evidence_text: str) -> CheckResult:
    claimed = _material_numbers(claim.text)
    if not claimed:
        return CheckResult(kind="number", passed=True, note="no numbers")
    present = set(NUMBER_RE.findall(evidence_text))
    missing = [item for item in claimed if item not in present]
    if not missing:
        return CheckResult(kind="number", passed=True, note="numbers match", supporting_ids=list(claim.evidence_ids))
    verdict: Verdict = "contradicted" if present else "insufficient"
    return CheckResult(
        kind="number",
        passed=False,
        note=f"numbers {missing} not in evidence",
        verdict=verdict,
        contradicting_ids=list(claim.evidence_ids) if verdict == "contradicted" else [],
    )


def structural_claim_checks(
    claim: KnowledgeClaim,
    *,
    unit: KnowledgeUnit,
    transcript: TranscriptDocument,
    visual: VisualCatalogue,
    allowed: set[str],
    index: dict[str, str],
) -> tuple[CheckResult, CheckResult, CheckResult]:
    """Local ref/number/step checks. Independent per claim; safe to run in parallel."""

    return (
        check_unknown_refs(claim, allowed),
        check_numbers(claim, _join(claim.evidence_ids, index)),
        check_steps(claim, unit, transcript, visual),
    )


def check_steps(
    claim: KnowledgeClaim,
    unit: KnowledgeUnit,
    transcript: TranscriptDocument,
    visual: VisualCatalogue,
) -> CheckResult:
    relations = [
        relation
        for relation in unit.relations
        if relation.kind == "step_before" and claim.id in {relation.from_id, relation.to_id}
    ]
    if unit.kind != "procedure" and not relations:
        return CheckResult(kind="step", passed=True, note="not a sequenced step")
    by_id = {item.id: item for item in unit.claims}
    for relation in relations:
        start = by_id.get(relation.from_id)
        end = by_id.get(relation.to_id)
        if start is None or end is None:
            continue
        start_times = _claim_times(start, transcript, visual)
        end_times = _claim_times(end, transcript, visual)
        if start_times and end_times and min(start_times) > min(end_times):
            return CheckResult(
                kind="step",
                passed=False,
                note="procedure evidence is out of order",
                verdict="contradicted",
                contradicting_ids=list(dict.fromkeys([*start.evidence_ids, *end.evidence_ids])),
            )
    return CheckResult(kind="step", passed=True, note="step order holds")


def run_claim_checks(
    claim: KnowledgeClaim,
    *,
    unit: KnowledgeUnit,
    transcript: TranscriptDocument,
    visual: VisualCatalogue,
    allowed: set[str],
    index: dict[str, str],
    provider: Provider | None = None,
    cancel_event: Event | None = None,
) -> list[CheckResult]:
    aligned_ids = _aligned_claim_evidence_ids(claim, unit, transcript)
    temporal = CheckResult(kind="translation", passed=True, note="caption times align with unit")
    segment_ids = {segment.id for segment in transcript.segments}
    misaligned = [
        item
        for item in claim.evidence_ids
        if item in segment_ids and item not in aligned_ids
    ]
    if misaligned and not aligned_ids:
        temporal = CheckResult(
            kind="translation",
            passed=False,
            note="caption citations lack temporal overlap with unit span",
            verdict="insufficient",
        )
    evidence_text = _join(aligned_ids or claim.evidence_ids, index)
    return [
        check_unknown_refs(claim, allowed),
        temporal,
        check_grounding(
            claim,
            index,
            provider=provider,
            cancel_event=cancel_event,
            evidence_ids=aligned_ids or claim.evidence_ids,
        ),
        check_numbers(claim, evidence_text),
        check_steps(claim, unit, transcript, visual),
    ]


def _worst_verdict(checks: list[CheckResult]) -> tuple[Verdict, str, list[str], list[str]]:
    contradicting: list[str] = []
    notes: list[str] = []
    verdict: Verdict = "insufficient"
    rank = {"supported": 0, "insufficient": 1, "contradicted": 2}
    grounding = next((item for item in checks if item.kind == "grounding"), None)
    for check in checks:
        contradicting.extend(check.contradicting_ids)
        if not check.passed:
            notes.append(check.note)
            candidate = check.verdict or "insufficient"
            if rank[candidate] > rank[verdict]:
                verdict = candidate
    supporting = list(dict.fromkeys(grounding.supporting_ids if grounding and grounding.passed else []))
    if not any(not item.passed for item in checks) and grounding is not None and grounding.passed and supporting:
        return "supported", "evidence-grounded support", supporting, []
    if not notes:
        notes = ["no affirmative evidence support"]
    return verdict, "; ".join(notes)[:400], supporting, list(dict.fromkeys(contradicting))


def _unit_for_claim(knowledge: KnowledgeDocument, claim_id: str) -> KnowledgeUnit:
    for unit in knowledge.units:
        if any(claim.id == claim_id for claim in unit.claims):
            return unit
    raise KeyError(claim_id)


def _keep_insufficient_example_in_draft(
    claim_id: str,
    *,
    knowledge: KnowledgeDocument,
    verdicts: dict[str, ClaimVerdict],
    quality_mode: QualityMode,
) -> bool:
    if quality_mode != "draft":
        return False
    unit = _unit_for_claim(knowledge, claim_id)
    if unit.kind != "example":
        return False
    for sibling in knowledge.units:
        if sibling.topic_id != unit.topic_id or sibling.kind == "example":
            continue
        for claim in sibling.claims:
            verdict = verdicts.get(claim.id)
            if verdict is not None and verdict.verdict == "supported":
                return True
    return False


def _is_critical(claim: KnowledgeClaim, checks: list[CheckResult]) -> bool:
    if claim.provenance != "source":
        return False
    return any(not check.passed and check.kind in CRITICAL_KINDS for check in checks)


def _sample_kinds(claim: KnowledgeClaim, checks: list[CheckResult]) -> list[CheckKind]:
    kinds: list[CheckKind] = ["grounding"]
    if NUMBER_RE.search(claim.text):
        kinds.append("number")
    if claim.provenance == "generated-practice":
        kinds.append("generated_practice")
    for check in checks:
        if check.kind in {"step", "translation", "image_text", "contradiction", "unknown_ref"} and (
            not check.passed or check.kind == "step"
        ):
            kinds.append(check.kind)
    return list(dict.fromkeys(kinds))


def _replace_claim_text(knowledge: KnowledgeDocument, claim_id: str, text: str) -> KnowledgeDocument:
    units = []
    for unit in knowledge.units:
        claims = [
            claim.model_copy(update={"text": text}) if claim.id == claim_id else claim
            for claim in unit.claims
        ]
        units.append(unit.model_copy(update={"claims": claims}))
    return knowledge.model_copy(update={"units": units})


def _sync_claim_status(knowledge: KnowledgeDocument, verdicts: list[ClaimVerdict]) -> KnowledgeDocument:
    status = {item.claim_id: item.verdict for item in verdicts}
    units = []
    for unit in knowledge.units:
        claims = [
            claim.model_copy(update={"status": status.get(claim.id, claim.status)})
            for claim in unit.claims
        ]
        units.append(unit.model_copy(update={"claims": claims}))
    return knowledge.model_copy(update={"units": units})


_SENSE_ROLE_PREFIXES = (
    "用法一：原因・理由",
    "用法二：比例・単位",
    "用法三：关于",
)


def _is_protected_sense_summary(page: PageIntent) -> bool:
    """Keep claim ids aligned with compact 用法一/二/三 summary lines.

    Soft-failing the 用法三 advice claim must not leave the bullet in place
    while removing its claim id; bind would then drop the line.
    """

    if page.type != "summary" or not page.claim_ids or not page.body_points:
        return False
    points = [point.strip() for point in page.body_points if point.strip()]
    if not points or len(points) != len(page.claim_ids):
        return False
    return all(point.startswith(_SENSE_ROLE_PREFIXES) for point in points)


def _is_protected_connective_page(page: PageIntent, knowledge: KnowledgeDocument) -> bool:
    """True for a learner 接续 page on a ～につき / ～つき lesson.

    Soft-failed gloss claims must not delete this page. The pre-verify plan
    already decided the page is mandatory; formalization only keeps it.
    """

    if page.type != "content":
        return False
    marked = page.title.startswith("接续") or "mandatory-connective" in page.selection_reason
    if not marked:
        return False
    if is_tsuki_grammar_lesson(None, knowledge):
        return True
    blob = f"{page.title}\n{page.notes}\n" + "\n".join(page.body_points)
    return "につき" in blob or "～つき" in blob or "〜つき" in blob


def _summary_body_for_claims(page: PageIntent, keep: list[str]) -> list[str]:
    """Keep learner bullets index-aligned with the summary claims that remain.

    ``body_points[i]`` is the display line for ``claim_ids[i]``. Dropping a
    claim without its bullet leaves bind with more bullets than claim ids.
    """

    if page.type != "summary" or not page.body_points:
        return list(page.body_points)
    keep_ids = set(keep)
    aligned = [
        page.body_points[index]
        for index, claim_id in enumerate(page.claim_ids)
        if claim_id in keep_ids and index < len(page.body_points)
    ]
    # A bullet with no claim slot was not paired with a claim removed here.
    # Sense-summary realignment may attach the missing id; do not discard it.
    if len(page.body_points) > len(page.claim_ids):
        aligned.extend(page.body_points[len(page.claim_ids) :])
    return aligned


def formalize_plan(
    plan: EditorialPlan,
    *,
    verdicts: list[ClaimVerdict],
    knowledge: KnowledgeDocument,
    quality_mode: QualityMode,
    removed: list[str],
    transcript: TranscriptDocument | None = None,
    visual: VisualCatalogue | None = None,
    provider: Provider | None = None,
) -> EditorialPlan:
    supported = {item.claim_id for item in verdicts if item.verdict == "supported"}
    removed_ids = set(removed)
    pages: list[PageIntent] = []
    extra_omissions: list[Omission] = list(plan.omissions)
    for page in plan.pages:
        protected = _is_protected_connective_page(page, knowledge)
        protected_summary = _is_protected_sense_summary(page)
        if quality_mode == "evidence-only":
            keep = list(page.claim_ids)
            dropped = []
        elif quality_mode == "strict":
            keep = [claim_id for claim_id in page.claim_ids if claim_id in supported]
            dropped = [claim_id for claim_id in page.claim_ids if claim_id not in supported]
        else:
            dropped = [claim_id for claim_id in page.claim_ids if claim_id in removed_ids]
            keep = [claim_id for claim_id in page.claim_ids if claim_id not in removed_ids]
        if protected or protected_summary:
            # Soft-failed 接续 claims, and claims paired with compact 用法 lines,
            # stay on the page so bind still has one claim per learner bullet.
            keep = list(page.claim_ids)
            dropped = []
        for claim_id in dropped:
            extra_omissions.append(Omission(claim_id=claim_id, reason="removed after verification"))
        if page.claim_ids and not keep and page.type in {"content", "summary"}:
            continue
        if quality_mode == "evidence-only":
            label = "evidence-only"
        elif quality_mode == "strict":
            label = "verified"
        else:
            label = "draft"
        if (protected or protected_summary) and quality_mode != "evidence-only" and any(
            claim_id not in supported for claim_id in keep
        ):
            label = "draft"
        updated = page.model_copy(
            update={
                "claim_ids": keep,
                "body_points": _summary_body_for_claims(page, keep),
            }
        )
        if _is_practice(knowledge, keep) and updated.type == "content":
            updated = updated.model_copy(update={"type": "quiz"})
        pages.append(relabel_page(updated, label))
    if not pages:
        pages = [relabel_page(plan.pages[0], "draft" if quality_mode != "evidence-only" else "evidence-only")]
    if quality_mode != "evidence-only" and transcript is not None and visual is not None:
        copy_index = evidence_index(transcript, visual)
        copy_ok = pages_copy_grounded(
            pages,
            knowledge=knowledge,
            transcript=transcript,
            visual=visual,
            provider=provider,
            index=copy_index,
        )
        checked: list[PageIntent] = []
        for page in pages:
            if page.type in {"content", "quiz"} and not copy_ok.get(page.id, False):
                checked.append(relabel_page(page, "draft"))
            else:
                checked.append(page)
        pages = checked
    from yt2class.stages.edit_deck import _realign_sense_summary

    pages = _realign_sense_summary(pages, course_map=None, knowledge=knowledge)
    kept_ids = {claim_id for page in pages for claim_id in page.claim_ids}
    extra_omissions = [
        item
        for item in extra_omissions
        if not (
            item.claim_id in kept_ids and item.reason == "removed after verification"
        )
    ]
    return plan.model_copy(update={"pages": pages, "omissions": extra_omissions})


def page_copy_grounded(
    page: PageIntent,
    *,
    knowledge: KnowledgeDocument,
    transcript: TranscriptDocument,
    visual: VisualCatalogue,
    provider: Provider | None = None,
    index: dict[str, str] | None = None,
) -> bool:
    """True when page title/notes/body_points are affirmed by the page's evidence."""

    return pages_copy_grounded(
        [page],
        knowledge=knowledge,
        transcript=transcript,
        visual=visual,
        provider=provider,
        index=index,
    ).get(page.id, False)


def pages_copy_grounded(
    pages: list[PageIntent],
    *,
    knowledge: KnowledgeDocument,
    transcript: TranscriptDocument,
    visual: VisualCatalogue,
    provider: Provider | None = None,
    index: dict[str, str] | None = None,
) -> dict[str, bool]:
    """Batch grounding for page copy strings (one verifier batch per evidence block)."""

    if index is None:
        index = evidence_index(transcript, visual)
    results = {page.id: True for page in pages}
    copy_claims: list[KnowledgeClaim] = []
    claim_pages: dict[str, str] = {}
    grounding_index = dict(index)
    for page in pages:
        if page.type not in {"content", "quiz"}:
            continue
        evidence_text = page_evidence_text(
            page, knowledge=knowledge, transcript=transcript, visual=visual, index=index
        )
        if not evidence_text.strip():
            results[page.id] = False
            continue
        excerpt_id = f"excerpt:{page.id}"
        grounding_index[excerpt_id] = evidence_text
        practice = _is_practice(knowledge, list(page.claim_ids))
        texts = [page.title, *page.body_points, notes_without_quality(page.notes)]
        for idx, text in enumerate(texts):
            if not text.strip():
                continue
            claim_id = f"copy:{page.id}:{idx}"
            claim = KnowledgeClaim(
                id=claim_id,
                status="insufficient",
                text=text,
                evidence_ids=[excerpt_id],
                provenance="generated-practice" if practice else "source",
            )
            if not check_numbers(claim, evidence_text).passed:
                results[page.id] = False
                continue
            copy_claims.append(claim)
            claim_pages[claim_id] = page.id
    if not copy_claims:
        return results
    grounding_map = check_grounding_batch(copy_claims, grounding_index, provider=provider)
    for claim_id, page_id in claim_pages.items():
        if not grounding_map.get(claim_id, _grounding_failure()).passed:
            results[page_id] = False
    return results


def _is_practice(knowledge: KnowledgeDocument, claim_ids: list[str]) -> bool:
    claims = {claim.id: claim for claim in knowledge.iter_claims()}
    chosen = [claims[item] for item in claim_ids if item in claims]
    return bool(chosen) and all(claim.provenance == "generated-practice" for claim in chosen)


def _complete_verifier(
    provider: Provider | None,
    payload: dict[str, Any],
    *,
    request_id: str,
    cancel_event: Event | None,
) -> dict[str, Any] | None:
    if provider is None:
        return None
    request = model_request(request_id=request_id[:60], role="verifier", payload=payload)
    request = request.model_copy(update={"request_id": f"{request_id[:60]}:{request.payload_digest[:32]}"})
    attach_provider_payload(provider, payload)
    try:
        result = provider.complete(request, cancel_event=cancel_event)
    except (BudgetExceeded, RequestCancelled):
        raise
    except Exception:
        return None
    structured = result.structured
    return structured if isinstance(structured, dict) else None


def _procedure_claim_ids(knowledge: KnowledgeDocument) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for unit in knowledge.units:
        if unit.kind == "procedure" or any(relation.kind == "step_before" for relation in unit.relations):
            groups[unit.id] = [claim.id for claim in unit.claims]
    return groups


def verify_claims(
    knowledge: KnowledgeDocument,
    *,
    plan: EditorialPlan,
    transcript: TranscriptDocument,
    visual: VisualCatalogue,
    provider: Provider | None = None,
    quality_mode: QualityMode = "draft",
    existing: VerificationReport | None = None,
    only_claim_ids: set[str] | None = None,
    cancel_event: Event | None = None,
) -> VerifyOutcome:
    """Verify claims. One repair; still-failing claims leave the formal deck."""

    allowed = allowed_evidence_ids(transcript, visual)
    index = evidence_index(transcript, visual)
    claim_map = {claim.id: claim for claim in knowledge.iter_claims()}
    target_ids = set(only_claim_ids or claim_map)
    repaired = False
    structural_errors: list[str] = []
    coverage_gaps: list[str] = []
    pending: list[str] = []
    removed: list[str] = []
    repaired_ids: list[str] = list(existing.repaired_claim_ids) if existing is not None else []
    check_by_claim: dict[str, list[CheckResult]] = {}
    verdicts: dict[str, ClaimVerdict] = {}

    if existing is not None:
        for item in existing.verdicts:
            if item.claim_id not in target_ids:
                verdicts[item.claim_id] = item
        pending.extend(item for item in existing.pending_review if item not in target_ids)
        removed.extend(item for item in existing.removed_from_formal if item not in target_ids)

    current_knowledge = knowledge
    claim_rows: list[tuple[str, KnowledgeClaim, KnowledgeUnit]] = []
    for claim_id in [item.id for item in knowledge.iter_claims() if item.id in target_ids]:
        claim = next(item for item in current_knowledge.iter_claims() if item.id == claim_id)
        unit = _unit_for_claim(current_knowledge, claim_id)
        claim_rows.append((claim_id, claim, unit))

    def _structural_row(
        row: tuple[str, KnowledgeClaim, KnowledgeUnit],
    ) -> tuple[str, CheckResult, CheckResult, CheckResult, CheckResult, list[str]]:
        claim_id, claim, unit = row
        if cancel_event is not None and cancel_event.is_set():
            raise RequestCancelled(f"verify cancelled before {claim_id}")
        ref_check = check_unknown_refs(claim, allowed)
        aligned_ids = _aligned_claim_evidence_ids(claim, unit, transcript)
        segment_ids = {segment.id for segment in transcript.segments}
        misaligned = [
            item
            for item in claim.evidence_ids
            if item in segment_ids and item not in aligned_ids
        ]
        temporal = CheckResult(
            kind="translation",
            passed=True,
            note="caption times align with unit",
        )
        if misaligned and not aligned_ids:
            temporal = CheckResult(
                kind="translation",
                passed=False,
                note="caption citations lack temporal overlap with unit span",
                verdict="insufficient",
            )
        cited_ids = aligned_ids or list(claim.evidence_ids)
        numbers = check_numbers(claim, _join(cited_ids, index))
        steps = check_steps(claim, unit, transcript, visual)
        return claim_id, ref_check, temporal, numbers, steps, cited_ids

    grounding_targets: list[KnowledgeClaim] = []
    grounding_evidence_ids: dict[str, list[str]] = {}
    preliminary: dict[str, list[CheckResult]] = {}
    claim_by_id = {claim_id: claim for claim_id, claim, _unit in claim_rows}
    for claim_id, ref_check, temporal, numbers, steps, cited_ids in map_parallel(
        claim_rows, _structural_row, cancel_event=cancel_event
    ):
        if not ref_check.passed:
            preliminary[claim_id] = [
                ref_check,
                temporal,
                _grounding_failure("skipped after unknown evidence ref"),
                numbers,
                steps,
            ]
        else:
            preliminary[claim_id] = [ref_check, temporal, numbers, steps]
            grounding_targets.append(claim_by_id[claim_id])
            grounding_evidence_ids[claim_id] = cited_ids

    grounding_map = check_grounding_batch(
        grounding_targets,
        index,
        provider=provider,
        cancel_event=cancel_event,
        evidence_ids_by_claim=grounding_evidence_ids,
    )

    for claim_id, claim, unit in claim_rows:
        parts = preliminary[claim_id]
        if len(parts) == 4:
            checks = [parts[0], parts[1], grounding_map[claim_id], parts[2], parts[3]]
        else:
            checks = parts
        check_by_claim[claim_id] = checks
        if any(check.kind == "unknown_ref" and not check.passed for check in checks):
            structural_errors.append(next(check.note for check in checks if check.kind == "unknown_ref"))
        verdict, reason, supporting, contradicting = _worst_verdict(checks)
        verdicts[claim_id] = ClaimVerdict(
            claim_id=claim_id,
            verdict=verdict,
            supporting_ids=supporting,
            contradicting_ids=contradicting,
            reason=reason,
        )

    procedure_groups = _procedure_claim_ids(current_knowledge)
    failed_ids = [
        claim_id
        for claim_id, verdict in verdicts.items()
        if claim_id in target_ids and verdict.verdict != "supported"
    ]
    already_repaired = set(repaired_ids)
    repair_targets = [claim_id for claim_id in failed_ids if claim_id not in already_repaired]
    unit_by_claim = {claim_id: unit for claim_id, _claim, unit in claim_rows}

    def _attempt_repair(claim_id: str) -> tuple[str, str | None]:
        if cancel_event is not None and cancel_event.is_set():
            raise RequestCancelled(f"verify cancelled before repair {claim_id}")
        claim = next(item for item in current_knowledge.iter_claims() if item.id == claim_id)
        repair_payload = {
            "prompt": load_prompt("verifier.md"),
            "claim": {"id": claim.id, "text": claim.text, "evidence_ids": claim.evidence_ids},
            "evidence_text": _join(claim.evidence_ids, index),
            "failed_checks": [check.note for check in check_by_claim.get(claim_id, []) if not check.passed],
            "allowed_evidence_ids": sorted(allowed),
        }
        repaired_structured = _complete_verifier(
            provider,
            repair_payload,
            request_id=f"verifier:repair:{claim_id}",
            cancel_event=cancel_event,
        )
        new_text = repaired_structured.get("text") if repaired_structured else None
        if isinstance(new_text, str) and new_text.strip() and new_text != claim.text:
            return claim_id, new_text.strip()[:4000]
        return claim_id, None

    repair_attempts = map_parallel(repair_targets, _attempt_repair, cancel_event=cancel_event)
    if repair_attempts:
        repaired = True
    recheck_ids: list[str] = []
    for claim_id, new_text in repair_attempts:
        repaired_ids.append(claim_id)
        already_repaired.add(claim_id)
        if new_text:
            current_knowledge = _replace_claim_text(current_knowledge, claim_id, new_text)
            recheck_ids.append(claim_id)

    def _recheck_repaired(claim_id: str) -> tuple[str, list[CheckResult]]:
        if cancel_event is not None and cancel_event.is_set():
            raise RequestCancelled(f"verify cancelled before recheck {claim_id}")
        claim = next(item for item in current_knowledge.iter_claims() if item.id == claim_id)
        checks = run_claim_checks(
            claim,
            unit=unit_by_claim[claim_id],
            transcript=transcript,
            visual=visual,
            allowed=allowed,
            index=index,
            provider=provider,
            cancel_event=cancel_event,
        )
        return claim_id, checks

    for claim_id, checks in map_parallel(recheck_ids, _recheck_repaired, cancel_event=cancel_event):
        check_by_claim[claim_id] = checks
        verdict, reason, supporting, contradicting = _worst_verdict(checks)
        verdicts[claim_id] = ClaimVerdict(
            claim_id=claim_id,
            verdict=verdict,
            supporting_ids=supporting,
            contradicting_ids=contradicting,
            reason=reason,
        )

    for unit_id, claim_ids in procedure_groups.items():
        if any(verdicts.get(claim_id) and verdicts[claim_id].verdict != "supported" for claim_id in claim_ids):
            for claim_id in claim_ids:
                if claim_id not in pending:
                    pending.append(claim_id)
                if claim_id not in removed:
                    removed.append(claim_id)

    for claim_id, verdict in list(verdicts.items()):
        if verdict.verdict == "supported":
            continue
        if _keep_insufficient_example_in_draft(
            claim_id,
            knowledge=current_knowledge,
            verdicts=verdicts,
            quality_mode=quality_mode,
        ):
            continue
        if claim_id not in removed:
            removed.append(claim_id)
        claim = next(item for item in current_knowledge.iter_claims() if item.id == claim_id)
        checks = check_by_claim.get(claim_id, [])
        if _is_critical(claim, checks) and claim_id not in pending:
            pending.append(claim_id)

    if quality_mode == "evidence-only":
        verdicts = {
            claim_id: item.model_copy(
                update={
                    "verdict": "insufficient" if item.verdict == "supported" else item.verdict,
                    "reason": "evidence-only mode does not emit supported labels",
                    "supporting_ids": [],
                }
            )
            if item.verdict == "supported"
            else item
            for claim_id, item in verdicts.items()
        }
        removed = [claim.id for claim in current_knowledge.iter_claims()]
        pending = []

    human_samples: list[HumanSample] = []
    for claim in current_knowledge.iter_claims():
        kinds = _sample_kinds(claim, check_by_claim.get(claim.id, []))
        if kinds:
            human_samples.append(
                HumanSample(
                    claim_id=claim.id,
                    reason="critical or check-bearing claim reserved for human sampling",
                    check_kinds=kinds[:12],
                )
            )
    if not human_samples and current_knowledge.iter_claims():
        first = current_knowledge.iter_claims()[0]
        human_samples.append(
            HumanSample(claim_id=first.id, reason="baseline human sample", check_kinds=["negation"])
        )

    requested_mode = quality_mode
    unresolved_critical = [
        claim.id
        for claim in current_knowledge.iter_claims()
        if claim.provenance == "source"
        and verdicts.get(claim.id)
        and verdicts[claim.id].verdict != "supported"
        and _is_critical(claim, check_by_claim.get(claim.id, []))
    ]
    emit_mode: QualityMode = requested_mode
    all_claim_ids = {claim.id for claim in current_knowledge.iter_claims()}
    if requested_mode == "strict" and (
        unresolved_critical
        or any(item.verdict != "supported" for item in verdicts.values())
        or set(verdicts) != all_claim_ids
    ):
        emit_mode = "draft"

    report = VerificationReport(
        schema_version="1.0",
        source_id=knowledge.source_id,
        quality_mode=emit_mode,
        verdicts=list(verdicts.values()),
        structural_errors=structural_errors[:20],
        pending_review=pending if emit_mode != "strict" else [],
        coverage_gaps=coverage_gaps,
        repaired_claim_ids=list(dict.fromkeys(repaired_ids)),
        removed_from_formal=list(dict.fromkeys(removed)) if emit_mode != "strict" else [],
        human_samples=human_samples,
        human_sampling_required=True,
    )
    current_knowledge = _sync_claim_status(current_knowledge, report.verdicts)
    formal_mode: QualityMode = "evidence-only" if requested_mode == "evidence-only" else emit_mode
    formal = formalize_plan(
        plan,
        verdicts=report.verdicts,
        knowledge=current_knowledge,
        quality_mode=formal_mode,
        removed=report.removed_from_formal,
        transcript=transcript,
        visual=visual,
        provider=provider,
    )
    outcome = VerifyOutcome(
        report=report,
        knowledge=current_knowledge,
        plan=formal,
        repaired=repaired,
        verified_claim_ids=sorted(target_ids),
    )
    if requested_mode == "strict" and emit_mode != "strict":
        raise StrictVerificationError(
            "strict verification cannot emit verified labels for unresolved critical claims",
            outcome=outcome,
        )
    return outcome

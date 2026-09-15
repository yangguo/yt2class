"""Claim verifier: structured fact checks, one repair, then formalize the deck."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from threading import Event
from typing import Any, Iterable

from yt2class.adapters.providers.base import Provider, RequestCancelled
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
from yt2class.stages.llm_util import (
    allowed_evidence_ids,
    attach_provider_payload,
    load_prompt,
    model_request,
)

NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
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
) -> str:
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


def _grounding_from_row(claim: KnowledgeClaim, index: dict[str, str], row: dict[str, Any]) -> CheckResult:
    failure = _grounding_failure()
    try:
        result = ClaimVerdict.model_validate(row)
    except (ValueError, TypeError):
        return failure
    refs = result.supporting_ids + result.contradicting_ids
    if result.claim_id != claim.id or any(
        item not in claim.evidence_ids or not index.get(item, "").strip() for item in refs
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
) -> dict[str, CheckResult]:
    """Batch grounding checks using the verifier contract's multi-claim payload."""

    results: dict[str, CheckResult] = {}
    eligible: list[KnowledgeClaim] = []
    for claim in claims:
        if not claim.text.strip() or not claim.evidence_ids or not any(
            index.get(item, "").strip() for item in claim.evidence_ids
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
        evidence_ids = sorted({item for claim in chunk for item in claim.evidence_ids})
        payload = {
            "prompt": load_prompt("verifier.md"),
            "claims": [claim.model_dump(mode="json") for claim in chunk],
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
                    claim, index, provider=provider, cancel_event=cancel_event
                )
            continue
        by_id = {str(row.get("claim_id")): row for row in rows if isinstance(row, dict)}
        for claim in chunk:
            row = by_id.get(claim.id)
            if row is None:
                results[claim.id] = _grounding_failure()
            else:
                results[claim.id] = _grounding_from_row(claim, index, row)
    return results


def _check_grounding_single(
    claim: KnowledgeClaim,
    index: dict[str, str],
    *,
    provider: Provider | None = None,
    cancel_event: Event | None = None,
) -> CheckResult:
    """One claim, one verifier request (batch fallback and public single-check API)."""
    if not claim.text.strip() or not claim.evidence_ids or not any(
        index.get(item, "").strip() for item in claim.evidence_ids
    ):
        return _grounding_failure("missing or empty cited evidence")
    if provider is None:
        return _grounding_failure()
    payload = {
        "prompt": load_prompt("verifier.md"),
        "claims": [claim.model_dump(mode="json")],
        "evidence": {item: index.get(item, "") for item in claim.evidence_ids},
        "allowed_evidence_ids": list(claim.evidence_ids),
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
    return _grounding_from_row(claim, index, rows[0])


def check_grounding(
    claim: KnowledgeClaim,
    index: dict[str, str],
    *,
    provider: Provider | None = None,
    cancel_event: Event | None = None,
) -> CheckResult:
    """Only a structured provider verdict can affirm free-text entailment."""
    return _check_grounding_single(
        claim, index, provider=provider, cancel_event=cancel_event
    )


def check_numbers(claim: KnowledgeClaim, evidence_text: str) -> CheckResult:
    claimed = NUMBER_RE.findall(claim.text)
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
    evidence_text = _join(claim.evidence_ids, index)
    return [
        check_unknown_refs(claim, allowed),
        check_grounding(claim, index, provider=provider, cancel_event=cancel_event),
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
    pages: list[PageIntent] = []
    extra_omissions: list[Omission] = list(plan.omissions)
    for page in plan.pages:
        if quality_mode == "evidence-only":
            keep = list(page.claim_ids)
            dropped = []
        elif quality_mode == "strict":
            keep = [claim_id for claim_id in page.claim_ids if claim_id in supported]
            dropped = [claim_id for claim_id in page.claim_ids if claim_id not in supported]
        else:
            dropped = [claim_id for claim_id in page.claim_ids if claim_id in set(removed)]
            keep = [claim_id for claim_id in page.claim_ids if claim_id not in set(removed)]
        for claim_id in dropped:
            extra_omissions.append(Omission(claim_id=claim_id, reason="removed after verification"))
        if page.type == "content" and page.claim_ids and not keep:
            continue
        if quality_mode == "evidence-only":
            label = "evidence-only"
        elif quality_mode == "strict":
            label = "verified"
        else:
            label = "draft"
        updated = page.model_copy(update={"claim_ids": keep})
        if _is_practice(knowledge, keep) and updated.type == "content":
            updated = updated.model_copy(update={"type": "quiz"})
        pages.append(relabel_page(updated, label))
    if not pages:
        pages = [relabel_page(plan.pages[0], "draft" if quality_mode != "evidence-only" else "evidence-only")]
    if quality_mode != "evidence-only" and transcript is not None and visual is not None:
        checked: list[PageIntent] = []
        for page in pages:
            if page.type in {"content", "quiz"} and not page_copy_grounded(
                page, knowledge=knowledge, transcript=transcript, visual=visual, provider=provider
            ):
                checked.append(relabel_page(page, "draft"))
            else:
                checked.append(page)
        pages = checked
    return plan.model_copy(update={"pages": pages, "omissions": extra_omissions})


def page_copy_grounded(
    page: PageIntent,
    *,
    knowledge: KnowledgeDocument,
    transcript: TranscriptDocument,
    visual: VisualCatalogue,
    provider: Provider | None = None,
) -> bool:
    """True when page title/notes/body_points are affirmed by the page's evidence."""

    evidence_text = page_evidence_text(
        page, knowledge=knowledge, transcript=transcript, visual=visual
    )
    if not evidence_text.strip():
        return False
    texts = [page.title, *page.body_points, notes_without_quality(page.notes)]
    practice = _is_practice(knowledge, list(page.claim_ids))
    return all(copy_is_affirmed(text, evidence_text, practice=practice, provider=provider) for text in texts)


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
    except RequestCancelled:
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

    grounding_targets: list[KnowledgeClaim] = []
    preliminary: dict[str, list[CheckResult]] = {}
    for claim_id, claim, unit in claim_rows:
        ref_check = check_unknown_refs(claim, allowed)
        numbers = check_numbers(claim, _join(claim.evidence_ids, index))
        steps = check_steps(claim, unit, transcript, visual)
        if not ref_check.passed:
            preliminary[claim_id] = [
                ref_check,
                _grounding_failure("skipped after unknown evidence ref"),
                numbers,
                steps,
            ]
        else:
            preliminary[claim_id] = [ref_check, numbers, steps]
            grounding_targets.append(claim)

    grounding_map = check_grounding_batch(
        grounding_targets,
        index,
        provider=provider,
        cancel_event=cancel_event,
    )

    for claim_id, claim, unit in claim_rows:
        parts = preliminary[claim_id]
        if len(parts) == 3:
            checks = [parts[0], grounding_map[claim_id], parts[1], parts[2]]
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
    for claim_id in failed_ids:
        if claim_id in already_repaired:
            continue
        claim = next(item for item in current_knowledge.iter_claims() if item.id == claim_id)
        unit = _unit_for_claim(current_knowledge, claim_id)
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
        repaired = True
        repaired_ids.append(claim_id)
        already_repaired.add(claim_id)
        new_text = None
        if repaired_structured:
            new_text = repaired_structured.get("text")
        if isinstance(new_text, str) and new_text.strip() and new_text != claim.text:
            current_knowledge = _replace_claim_text(current_knowledge, claim_id, new_text.strip()[:4000])
            claim = next(item for item in current_knowledge.iter_claims() if item.id == claim_id)
            checks = run_claim_checks(
                claim,
                unit=unit,
                transcript=transcript,
                visual=visual,
                allowed=allowed,
                index=index,
                provider=provider,
                cancel_event=cancel_event,
            )
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

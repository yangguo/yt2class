"""Claim verifier: structured fact checks, one repair, then formalize the deck."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from threading import Event
from typing import Any, Iterable

from yt2class.adapters.providers.base import Provider
from yt2class.domain.editorial import EditorialPlan, Omission, PageIntent, relabel_page
from yt2class.domain.knowledge import KnowledgeClaim, KnowledgeDocument, KnowledgeUnit
from yt2class.domain.transcript import TranscriptDocument
from yt2class.domain.verification import (
    CheckKind,
    ClaimVerdict,
    HumanSample,
    QualityMode,
    Verdict,
    VerificationReport,
)
from yt2class.domain.visual import VisualCatalogue
from yt2class.stages.llm_util import (
    allowed_evidence_ids,
    load_prompt,
    model_request,
    text_has_negation,
)
from yt2class.stages.reduce_knowledge import normalize_concept, polarity

NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
CONDITION_MARKERS = (
    "如果",
    "若",
    "除非",
    "只有",
    "当且仅当",
    "if ",
    "unless",
    "only if",
    "when ",
)
PROPER_NAME_RE = re.compile(r"\b[A-Z][a-zA-Z]{1,}\b")
KANA_RE = re.compile(r"[\u3040-\u30ff]")
JP_TERM_RE = re.compile(r"[\u30a0-\u30ff]{2,}|[\u4e00-\u9fff]{2,}")
SIMPLIFY = str.maketrans("動詞語彙導體學時長間後前後", "动词语汇导体学时长间后前后")
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
}


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
        if self.report.pending_review or self.report.structural_errors:
            return False
        return all(item.verdict == "supported" for item in self.report.verdicts)


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


def _transcript_text(claim: KnowledgeClaim, transcript: TranscriptDocument) -> str:
    by_id = {segment.id: segment.text_original for segment in transcript.segments}
    return " ".join(by_id[item] for item in claim.evidence_ids if item in by_id)


def _ocr_text(claim: KnowledgeClaim, visual: VisualCatalogue) -> str:
    frame_ids = set(claim.evidence_ids)
    return " ".join(
        region.text
        for region in visual.ocr_regions
        if region.id in frame_ids or region.parent_occurrence_id in frame_ids
    )


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


def check_unknown_refs(claim: KnowledgeClaim, allowed: set[str]) -> CheckResult:
    missing = [item for item in claim.evidence_ids if item not in allowed]
    if missing:
        return CheckResult(
            kind="unknown_ref",
            passed=False,
            note=f"unknown evidence refs {missing}",
            verdict="insufficient",
        )
    return CheckResult(kind="unknown_ref", passed=True, note="refs resolve", supporting_ids=list(claim.evidence_ids))


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


def check_negation(claim: KnowledgeClaim, evidence_text: str) -> CheckResult:
    claim_neg = text_has_negation(claim.text)
    evidence_neg = text_has_negation(evidence_text)
    if evidence_neg and not claim_neg:
        return CheckResult(
            kind="negation",
            passed=False,
            note="claim dropped a source negation",
            verdict="contradicted",
            contradicting_ids=list(claim.evidence_ids),
        )
    if claim_neg and not evidence_neg:
        return CheckResult(
            kind="negation",
            passed=False,
            note="claim negation is not in evidence",
            verdict="insufficient",
        )
    return CheckResult(kind="negation", passed=True, note="negation aligned")


def check_conditions(claim: KnowledgeClaim, evidence_text: str) -> CheckResult:
    ev = f" {evidence_text} "
    cl = f" {claim.text} "
    ev_has = any(marker in ev or marker in evidence_text for marker in CONDITION_MARKERS)
    cl_has = any(marker in cl or marker in claim.text for marker in CONDITION_MARKERS)
    if ev_has and not cl_has:
        return CheckResult(
            kind="condition",
            passed=False,
            note="claim dropped a source condition",
            verdict="insufficient",
        )
    return CheckResult(kind="condition", passed=True, note="conditions aligned")


def check_proper_names(claim: KnowledgeClaim, evidence_text: str) -> CheckResult:
    names = PROPER_NAME_RE.findall(claim.text)
    if not names:
        return CheckResult(kind="proper_name", passed=True, note="no proper names")
    lowered = evidence_text.lower()
    missing = [name for name in names if name.lower() not in lowered]
    if missing:
        others = PROPER_NAME_RE.findall(evidence_text)
        verdict: Verdict = "contradicted" if others else "insufficient"
        return CheckResult(
            kind="proper_name",
            passed=False,
            note=f"proper names {missing} not in evidence",
            verdict=verdict,
            contradicting_ids=list(claim.evidence_ids) if verdict == "contradicted" else [],
        )
    return CheckResult(kind="proper_name", passed=True, note="names match")


def check_translation(claim: KnowledgeClaim, transcript_text: str) -> CheckResult:
    if not transcript_text or not KANA_RE.search(transcript_text):
        return CheckResult(kind="translation", passed=True, note="no source-language term check")
    terms = [term for term in JP_TERM_RE.findall(transcript_text) if len(term) >= 2]
    if not terms:
        return CheckResult(kind="translation", passed=True, note="no extractable source terms")
    retained = any(term in claim.text or term.translate(SIMPLIFY) in claim.text for term in terms)
    # Source-script terms themselves must remain; a CJK gloss alone is not enough.
    if any(term in claim.text for term in terms):
        return CheckResult(kind="translation", passed=True, note="source terms retained")
    if retained and KANA_RE.search(claim.text):
        return CheckResult(kind="translation", passed=True, note="source script retained")
    return CheckResult(
        kind="translation",
        passed=False,
        note="translation dropped source-language terms",
        verdict="insufficient",
    )


def check_image_text(
    claim: KnowledgeClaim,
    *,
    ocr_text: str,
    evidence_text: str,
    visual: VisualCatalogue,
) -> CheckResult:
    cited_frames = [item for item in claim.evidence_ids if any(occ.id == item for occ in visual.occurrences)]
    if not cited_frames:
        return CheckResult(kind="image_text", passed=True, note="no frames cited")
    numbers = NUMBER_RE.findall(claim.text)
    combined = f"{ocr_text} {evidence_text}"
    missing = [item for item in numbers if item not in combined]
    if missing:
        return CheckResult(
            kind="image_text",
            passed=False,
            note=f"image/text mismatch for {missing}",
            verdict="insufficient",
        )
    return CheckResult(kind="image_text", passed=True, note="image-text aligned", supporting_ids=cited_frames)


def check_contradiction(claim: KnowledgeClaim, index: dict[str, str]) -> CheckResult:
    pieces = [(item, index.get(item, "")) for item in claim.evidence_ids if index.get(item)]
    for left_id, left in pieces:
        for right_id, right in pieces:
            if left_id >= right_id:
                continue
            if normalize_concept(left) and normalize_concept(left) == normalize_concept(right):
                if polarity(left) != polarity(right):
                    return CheckResult(
                        kind="contradiction",
                        passed=False,
                        note="cited evidence contradicts itself",
                        verdict="contradicted",
                        contradicting_ids=[left_id, right_id],
                    )
    return CheckResult(kind="contradiction", passed=True, note="no internal contradiction")


def check_generated_practice(claim: KnowledgeClaim) -> CheckResult:
    if claim.provenance != "generated-practice":
        if "练习" in claim.text and "原视频" in claim.text:
            return CheckResult(
                kind="generated_practice",
                passed=False,
                note="practice disguised as source",
                verdict="contradicted",
            )
        return CheckResult(kind="generated_practice", passed=True, note="source claim")
    banned = ("原视频给出该题", "视频中出了这道题", "讲师原题")
    if any(marker in claim.text for marker in banned):
        return CheckResult(
            kind="generated_practice",
            passed=False,
            note="generated practice cannot claim to be from the video",
            verdict="contradicted",
        )
    return CheckResult(kind="generated_practice", passed=True, note="tagged generated-practice")


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
) -> list[CheckResult]:
    evidence_text = _join(claim.evidence_ids, index)
    transcript_text = _transcript_text(claim, transcript)
    ocr_text = _ocr_text(claim, visual)
    return [
        check_unknown_refs(claim, allowed),
        check_numbers(claim, evidence_text),
        check_negation(claim, evidence_text or transcript_text),
        check_conditions(claim, evidence_text or transcript_text),
        check_proper_names(claim, evidence_text or transcript_text),
        check_translation(claim, transcript_text),
        check_image_text(claim, ocr_text=ocr_text, evidence_text=evidence_text, visual=visual),
        check_contradiction(claim, index),
        check_generated_practice(claim),
        check_steps(claim, unit, transcript, visual),
    ]


def _worst_verdict(checks: list[CheckResult]) -> tuple[Verdict, str, list[str], list[str]]:
    supporting: list[str] = []
    contradicting: list[str] = []
    notes: list[str] = []
    verdict: Verdict = "supported"
    rank = {"supported": 0, "insufficient": 1, "contradicted": 2}
    for check in checks:
        supporting.extend(check.supporting_ids)
        contradicting.extend(check.contradicting_ids)
        if not check.passed:
            notes.append(check.note)
            candidate = check.verdict or "insufficient"
            if rank[candidate] > rank[verdict]:
                verdict = candidate
    if verdict == "supported":
        notes = ["deterministic checks passed"]
    return verdict, "; ".join(notes)[:400], list(dict.fromkeys(supporting)), list(dict.fromkeys(contradicting))


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
    kinds: list[CheckKind] = []
    if NUMBER_RE.search(claim.text):
        kinds.append("number")
    if text_has_negation(claim.text):
        kinds.append("negation")
    if any(marker in claim.text for marker in CONDITION_MARKERS):
        kinds.append("condition")
    if PROPER_NAME_RE.search(claim.text):
        kinds.append("proper_name")
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
    return plan.model_copy(update={"pages": pages, "omissions": extra_omissions})


def _is_practice(knowledge: KnowledgeDocument, claim_ids: list[str]) -> bool:
    claims = {claim.id: claim for claim in knowledge.iter_claims()}
    chosen = [claims[item] for item in claim_ids if item in claims]
    return bool(chosen) and all(claim.provenance == "generated-practice" for claim in chosen)


def _attach_payload(provider: Provider, payload: dict[str, Any]) -> None:
    if hasattr(provider, "last_payload"):
        provider.last_payload = payload


def _complete_verifier(
    provider: Provider,
    payload: dict[str, Any],
    *,
    request_id: str,
    cancel_event: Event | None,
) -> dict[str, Any] | None:
    request = model_request(request_id=request_id, role="verifier", payload=payload)
    _attach_payload(provider, payload)
    result = provider.complete(request, cancel_event=cancel_event)
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
    provider: Provider,
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
    repaired_ids: list[str] = []
    check_by_claim: dict[str, list[CheckResult]] = {}
    verdicts: dict[str, ClaimVerdict] = {}

    if existing is not None:
        for item in existing.verdicts:
            if item.claim_id not in target_ids:
                verdicts[item.claim_id] = item
        pending.extend(item for item in existing.pending_review if item not in target_ids)
        removed.extend(item for item in existing.removed_from_formal if item not in target_ids)
        repaired_ids.extend(item for item in existing.repaired_claim_ids if item not in target_ids)

    draft_verdicts: list[dict[str, Any]] = []
    current_knowledge = knowledge
    for claim_id in [item.id for item in knowledge.iter_claims() if item.id in target_ids]:
        claim = next(item for item in current_knowledge.iter_claims() if item.id == claim_id)
        unit = _unit_for_claim(current_knowledge, claim_id)
        checks = run_claim_checks(
            claim,
            unit=unit,
            transcript=transcript,
            visual=visual,
            allowed=allowed,
            index=index,
        )
        check_by_claim[claim_id] = checks
        if any(check.kind == "unknown_ref" and not check.passed for check in checks):
            structural_errors.append(next(check.note for check in checks if check.kind == "unknown_ref"))
        verdict, reason, supporting, contradicting = _worst_verdict(checks)
        draft_verdicts.append(
            {
                "claim_id": claim_id,
                "verdict": verdict,
                "supporting_ids": supporting,
                "contradicting_ids": contradicting,
                "reason": reason,
            }
        )
        verdicts[claim_id] = ClaimVerdict(
            claim_id=claim_id,
            verdict=verdict,
            supporting_ids=supporting,
            contradicting_ids=contradicting,
            reason=reason,
        )

    payload = {
        "prompt": load_prompt("verifier.md"),
        "quality_mode": quality_mode,
        "allowed_evidence_ids": sorted(allowed),
        "allowed_frame_ids": [occurrence.id for occurrence in visual.occurrences],
        "claims": [
            {
                "id": claim.id,
                "text": claim.text,
                "evidence_ids": claim.evidence_ids,
                "provenance": claim.provenance,
            }
            for claim in current_knowledge.iter_claims()
            if claim.id in target_ids
        ],
        "draft_verdicts": draft_verdicts,
        "constraints": {"model_agreement_is_not_sufficient": True},
    }
    structured = _complete_verifier(
        provider,
        payload,
        request_id=f"verifier:batch:{'-'.join(sorted(target_ids))[:80]}",
        cancel_event=cancel_event,
    )
    if structured and isinstance(structured.get("verdicts"), list):
        for raw in structured["verdicts"]:
            if not isinstance(raw, dict):
                continue
            claim_id = raw.get("claim_id")
            if claim_id not in verdicts:
                continue
            llm_verdict = raw.get("verdict")
            current = verdicts[claim_id]
            if current.verdict == "supported" and llm_verdict in {"contradicted", "insufficient"}:
                verdicts[claim_id] = current.model_copy(
                    update={"verdict": llm_verdict, "reason": str(raw.get("reason") or current.reason)[:400]}
                )

    procedure_groups = _procedure_claim_ids(current_knowledge)
    failed_ids = [
        claim_id
        for claim_id, verdict in verdicts.items()
        if claim_id in target_ids and verdict.verdict != "supported"
    ]
    for claim_id in failed_ids:
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
        new_text = None
        if repaired_structured:
            new_text = repaired_structured.get("text")
        if isinstance(new_text, str) and new_text.strip() and new_text != claim.text:
            current_knowledge = _replace_claim_text(current_knowledge, claim_id, new_text.strip()[:4000])
            repaired_ids.append(claim_id)
            claim = next(item for item in current_knowledge.iter_claims() if item.id == claim_id)
            checks = run_claim_checks(
                claim,
                unit=unit,
                transcript=transcript,
                visual=visual,
                allowed=allowed,
                index=index,
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
    if requested_mode == "strict" and (
        unresolved_critical or any(item.verdict != "supported" for item in verdicts.values())
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

"""Safe aliases for weaker model JSON. Never invent evidence IDs or timestamps."""

from __future__ import annotations

import re
from typing import Any

_NUMERIC = re.compile(r"^\d+(?:\.\d+)?$")

UNIT_KINDS = {
    "concept",
    "example",
    "procedure",
    "comparison",
    "warning",
    "recap",
}
KIND_ALIASES = {
    "definition": "concept",
    "fact": "concept",
    "explanation": "concept",
    "knowledge": "concept",
    "sample": "example",
    "instance": "example",
    "process": "procedure",
    "steps": "procedure",
    "step": "procedure",
    "contrast": "comparison",
    "versus": "comparison",
    "caution": "warning",
    "note": "warning",
    "review": "recap",
    "summary": "recap",
}
CLAIM_STATUSES = {
    "draft",
    "supported",
    "contradicted",
    "insufficient",
    "unresolved",
}
UNITS_LIST_KEYS = ("units", "knowledge_units", "knowledgeUnits", "items")
TOPICS_LIST_KEYS = ("topics", "outline", "sections")


def allocate_unique_id(preferred: str, used: set[str]) -> str:
    """Keep the first occurrence; suffix later collisions. Does not invent content."""

    base = (preferred or "id").strip()[:100] or "id"
    if base not in used:
        used.add(base)
        return base
    n = 2
    while True:
        suffix = f"-{n}"
        room = 100 - len(suffix)
        candidate = f"{base[:room]}{suffix}" if len(base) + len(suffix) > 100 else f"{base}{suffix}"
        if candidate not in used:
            used.add(candidate)
            return candidate
        n += 1


def _first(raw: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in raw and raw[key] is not None:
            return raw[key]
    return None


def coerce_identifier(value: Any) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        if 1 <= len(text) <= 100:
            return text
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        text = str(value)
        if 1 <= len(text) <= 100:
            return text
    return None


def coerce_seconds(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number < 0 or number != number or number == float("inf"):
            return None
        return number
    if isinstance(value, str):
        text = value.strip()
        if _NUMERIC.fullmatch(text):
            return float(text)
    return None


def coerce_id_list(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        ident = coerce_identifier(value)
        return [ident] if ident else []
    if not isinstance(value, list):
        return None
    ids: list[str] = []
    for item in value:
        if isinstance(item, dict):
            ident = coerce_identifier(item.get("id") or item.get("evidence_id"))
        else:
            ident = coerce_identifier(item)
        if ident:
            ids.append(ident)
    return ids


def coerce_interval(
    raw: dict[str, Any],
    *,
    fallback_start: float | None,
    fallback_end: float | None,
) -> tuple[float, float] | None:
    """Map time aliases; fill omitted bounds from a known window. Do not invent."""

    start = coerce_seconds(
        _first(raw, ("start_seconds", "startSeconds", "start", "start_time", "begin_seconds"))
    )
    end = coerce_seconds(
        _first(raw, ("end_seconds", "endSeconds", "end", "end_time", "finish_seconds"))
    )
    span = _first(raw, ("time_range", "timeRange", "range", "span"))
    if isinstance(span, (list, tuple)) and len(span) >= 2:
        if start is None:
            start = coerce_seconds(span[0])
        if end is None:
            end = coerce_seconds(span[1])
    elif isinstance(span, dict):
        if start is None:
            start = coerce_seconds(
                _first(span, ("start_seconds", "startSeconds", "start", "begin"))
            )
        if end is None:
            end = coerce_seconds(_first(span, ("end_seconds", "endSeconds", "end", "finish")))
    provided_both = start is not None and end is not None
    if not provided_both:
        if start is None:
            start = fallback_start
        if end is None:
            end = fallback_end
    if start is None or end is None or not start < end:
        return None
    return start, end


def _pick_keys(raw: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: raw[key] for key in keys if key in raw}


def extract_topics_list(structured: Any) -> list[Any] | None:
    if isinstance(structured, list):
        return structured
    if not isinstance(structured, dict):
        return None
    for key in TOPICS_LIST_KEYS:
        value = structured.get(key)
        if isinstance(value, list):
            return value
        if key in structured:
            return None
    return None


def extract_units_list(structured: Any) -> list[Any] | None:
    if isinstance(structured, list):
        return structured
    if not isinstance(structured, dict):
        return None
    for key in UNITS_LIST_KEYS:
        if key not in structured:
            continue
        value = structured[key]
        if isinstance(value, list):
            return value
        if isinstance(value, dict) and value and all(isinstance(item, dict) for item in value.values()):
            return list(value.values())
        return None
    if "claims" in structured or "kind" in structured:
        return [structured]
    return None


def coerce_topic(
    raw: dict[str, Any],
    *,
    block_id: str,
    block_start: float,
    block_end: float,
    index: int,
) -> dict[str, Any] | None:
    title_raw = _first(raw, ("title", "name", "heading", "topic"))
    if not isinstance(title_raw, str) or not title_raw.strip():
        return None
    title = title_raw.strip()[:160]
    goal_raw = _first(
        raw,
        (
            "goal",
            "student_goal",
            "studentGoal",
            "learning_goal",
            "learning_objective",
            "objective",
            "teaching_goal",
            "teachingGoal",
            "purpose",
        ),
    )
    goal = goal_raw.strip()[:400] if isinstance(goal_raw, str) and goal_raw.strip() else None
    if not goal:
        return None
    interval = coerce_interval(raw, fallback_start=block_start, fallback_end=block_end)
    if interval is None:
        return None
    ident = coerce_identifier(_first(raw, ("id", "topic_id", "topicId"))) or f"topic-{block_id}-{index:02d}"
    if len(ident) > 100:
        ident = ident[:100]
    evidence = coerce_id_list(
        _first(raw, ("evidence_ids", "evidenceIds", "evidence", "citations", "sources"))
    )
    speculative = _first(raw, ("speculative", "is_speculative", "isSpeculative"))
    dumped = {
        "id": ident,
        "title": title[:160],
        "goal": goal,
        "start_seconds": interval[0],
        "end_seconds": interval[1],
        "evidence_ids": evidence if evidence is not None else [],
    }
    if isinstance(speculative, bool):
        dumped["speculative"] = speculative
    elif not dumped["evidence_ids"]:
        dumped["speculative"] = True
    return dumped


def _coerce_kind(value: Any) -> str:
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in UNIT_KINDS:
            return lowered
        if lowered in KIND_ALIASES:
            return KIND_ALIASES[lowered]
    return "concept"


def coerce_claim(raw: dict[str, Any], *, index: int, unit_id: str) -> dict[str, Any] | None:
    text_raw = _first(raw, ("text", "claim", "statement", "content"))
    if not isinstance(text_raw, str) or not text_raw.strip():
        return None
    evidence = coerce_id_list(
        _first(raw, ("evidence_ids", "evidenceIds", "evidence", "citations", "sources", "evidence_id"))
    )
    if not evidence:
        return None
    ident = coerce_identifier(_first(raw, ("id", "claim_id", "claimId"))) or f"{unit_id}-c{index:02d}"
    status_raw = _first(raw, ("status", "claim_status"))
    status = status_raw.strip().lower() if isinstance(status_raw, str) else "draft"
    if status not in CLAIM_STATUSES:
        status = "draft"
    dumped = {
        "id": ident[:100],
        "text": text_raw.strip()[:4000],
        "evidence_ids": evidence[:20],
        "status": status,
    }
    qualifiers = raw.get("qualifiers")
    if isinstance(qualifiers, list):
        dumped["qualifiers"] = [str(item) for item in qualifiers if item][:20]
    modality = raw.get("modality")
    if modality in {"audio", "visual", "both"}:
        dumped["modality"] = modality
    provenance = raw.get("provenance")
    if provenance in {"source", "generated-practice"}:
        dumped["provenance"] = provenance
    return dumped


def _coerce_nested_interval_rows(
    rows: Any,
    *,
    keys: tuple[str, ...],
    fallback_start: float,
    fallback_end: float,
) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    cleaned: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        interval = coerce_interval(row, fallback_start=fallback_start, fallback_end=fallback_end)
        if interval is None:
            continue
        dumped = _pick_keys(row, keys)
        dumped["start_seconds"] = interval[0]
        dumped["end_seconds"] = interval[1]
        cleaned.append(dumped)
    return cleaned


def coerce_knowledge_unit(
    raw: dict[str, Any],
    *,
    payload: dict[str, Any],
    index: int,
) -> dict[str, Any] | None:
    context = payload.get("context_range") or [0.0, 0.0]
    fallback_start = coerce_seconds(context[0]) if len(context) >= 2 else 0.0
    fallback_end = coerce_seconds(context[1]) if len(context) >= 2 else None
    interval = coerce_interval(raw, fallback_start=fallback_start, fallback_end=fallback_end)
    if interval is None:
        return None
    segment_id = payload.get("segment_id")
    topic_id = (payload.get("course_context") or {}).get("topic_id")
    ident = coerce_identifier(_first(raw, ("id", "unit_id", "unitId"))) or (
        f"unit-{segment_id}-{index:02d}" if segment_id else f"unit-{index:02d}"
    )
    claims_raw = raw.get("claims")
    if not isinstance(claims_raw, list):
        return None
    claims = []
    used_claim_ids: set[str] = set()
    for claim_index, item in enumerate(claims_raw, start=1):
        if not isinstance(item, dict):
            continue
        claim = coerce_claim(item, index=claim_index, unit_id=ident[:80])
        if claim is not None:
            claim["id"] = allocate_unique_id(claim["id"], used_claim_ids)
            claims.append(claim)
    if not claims:
        return None
    segment_ids = coerce_id_list(_first(raw, ("segment_ids", "segmentIds", "segment_id")))
    if not segment_ids and isinstance(segment_id, str):
        segment_ids = [segment_id]
    unit_topic = coerce_identifier(_first(raw, ("topic_id", "topicId"))) or coerce_identifier(topic_id)
    if not unit_topic or not segment_ids:
        return None
    dumped: dict[str, Any] = {
        "id": ident[:100],
        "topic_id": unit_topic,
        "segment_ids": segment_ids[:20],
        "start_seconds": interval[0],
        "end_seconds": interval[1],
        "kind": _coerce_kind(_first(raw, ("kind", "type", "unit_kind", "unitKind"))),
        "claims": claims,
    }
    relations = raw.get("relations")
    if isinstance(relations, list):
        dumped["relations"] = [
            item
            for item in (
                _pick_keys(row, ("from_id", "to_id", "kind"))
                for row in relations
                if isinstance(row, dict)
            )
            if item.get("from_id")
            and item.get("to_id")
            and item.get("kind") in {"supports", "contrasts", "prerequisite", "step_before"}
        ]
    candidates = raw.get("visual_candidates") or raw.get("visualCandidates")
    if isinstance(candidates, list):
        kept_candidates: list[dict[str, Any]] = []
        for item in candidates:
            if not isinstance(item, dict):
                continue
            frame_id = coerce_identifier(item.get("frame_id") or item.get("frameId"))
            relevance = coerce_seconds(item.get("relevance"))
            legibility = coerce_seconds(item.get("legibility"))
            reason = item.get("selection_reason") or item.get("selectionReason")
            if (
                frame_id
                and relevance is not None
                and legibility is not None
                and 0 <= relevance <= 1
                and 0 <= legibility <= 1
                and isinstance(reason, str)
                and reason.strip()
            ):
                kept_candidates.append(
                    {
                        "frame_id": frame_id,
                        "relevance": relevance,
                        "legibility": legibility,
                        "selection_reason": reason.strip()[:240],
                    }
                )
        dumped["visual_candidates"] = kept_candidates
    dumped["uncertainty"] = _coerce_nested_interval_rows(
        raw.get("uncertainty"),
        keys=("kind", "start_seconds", "end_seconds", "note"),
        fallback_start=interval[0],
        fallback_end=interval[1],
    )
    dumped["evidence_requests"] = _coerce_nested_interval_rows(
        raw.get("evidence_requests") or raw.get("evidenceRequests"),
        keys=("start_seconds", "end_seconds", "reason", "desired_modality"),
        fallback_start=interval[0],
        fallback_end=interval[1],
    )
    return dumped


def uniquify_knowledge_units(units: list[Any]) -> list[Any]:
    """Renumber colliding unit/claim ids, keeping the first occurrence."""

    from yt2class.domain.knowledge import KnowledgeUnit

    used_units: set[str] = set()
    used_claims: set[str] = set()
    result: list[Any] = []
    for unit in units:
        if not isinstance(unit, KnowledgeUnit):
            result.append(unit)
            continue
        new_unit_id = allocate_unique_id(unit.id, used_units)
        first_claim_new: dict[str, str] = {}
        new_claims = []
        for claim in unit.claims:
            new_claim_id = allocate_unique_id(claim.id, used_claims)
            first_claim_new.setdefault(claim.id, new_claim_id)
            new_claims.append(
                claim if new_claim_id == claim.id else claim.model_copy(update={"id": new_claim_id})
            )

        def _rewrite(ref: str, *, old_unit: str = unit.id, new_unit: str = new_unit_id) -> str:
            if ref == old_unit:
                return new_unit
            return first_claim_new.get(ref, ref)

        new_relations = [
            relation
            if _rewrite(relation.from_id) == relation.from_id
            and _rewrite(relation.to_id) == relation.to_id
            else relation.model_copy(
                update={"from_id": _rewrite(relation.from_id), "to_id": _rewrite(relation.to_id)}
            )
            for relation in unit.relations
        ]
        updates: dict[str, Any] = {}
        if new_unit_id != unit.id:
            updates["id"] = new_unit_id
        if any(left.id != right.id for left, right in zip(new_claims, unit.claims)):
            updates["claims"] = new_claims
        if any(
            left.from_id != right.from_id or left.to_id != right.to_id
            for left, right in zip(new_relations, unit.relations)
        ):
            updates["relations"] = new_relations
        result.append(unit.model_copy(update=updates) if updates else unit)
    return result


ROLE_JSON_REMINDERS = {
    "outline": (
        "Output a JSON object with keys topics, relations, unverified_guesses. "
        "Each topic MUST use exactly: id, title, goal, start_seconds, end_seconds, "
        "evidence_ids, speculative. Use `goal` (not teaching_goal or student_goal). "
        "Topic ids must be unique; do not reuse topic-0001."
    ),
    "segment": (
        "Output a JSON object whose top-level key is `units` (an array). Each unit MUST use: "
        "id, topic_id, segment_ids, start_seconds, end_seconds, kind, claims. "
        "Each claim MUST use: id, text, evidence_ids, status. kind is one of "
        "concept|example|procedure|comparison|warning|recap. "
        "Unit and claim ids must be unique across the course; do not reuse unit-0001. "
        "Do not invent evidence IDs."
    ),
    "editor": (
        "Output a JSON object for the editorial plan. Use the specified field names only."
    ),
    "verifier": (
        "Output a JSON object with key verdicts. Use the specified field names only."
    ),
}

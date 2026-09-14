"""Deterministic FakeProvider responders for M2 tests and the fake analyze CLI.

These functions invent no live-model gold answers. They only reshape already
supplied transcript/visual IDs into CourseMap and KnowledgeUnit JSON so the
vertical path can be exercised offline.
"""

from __future__ import annotations

from typing import Any

from yt2class.adapters.providers.base import FakeProvider, ModelRequest
from yt2class.stages.llm_util import text_has_negation, text_has_units


def _block_from_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    payload = payload or {}
    block = payload.get("block") or {}
    excerpts = list(payload.get("transcript") or [])
    frames = list(payload.get("visual_overview") or payload.get("frames") or [])
    allowed = list(payload.get("allowed_evidence_ids") or [])
    return {
        "id": block.get("id") or payload.get("segment_id") or "block-0001",
        "start": float(block.get("start_seconds", payload.get("core_range", [0.0, 1.0])[0])),
        "end": float(block.get("end_seconds", payload.get("core_range", [0.0, 1.0])[1])),
        "excerpts": excerpts,
        "frames": frames,
        "allowed": allowed,
        "evidence_ids": list(block.get("evidence_ids") or payload.get("evidence_ids") or []),
    }


def synthetic_outline_from_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    info = _block_from_payload(payload)
    excerpts = info["excerpts"]
    if not excerpts:
        return {
            "topics": [
                {
                    "id": f"topic-{info['id']}",
                    "title": "无字幕区间",
                    "goal": "记录缺失语音并保留可见画面",
                    "start_seconds": info["start"],
                    "end_seconds": info["end"],
                    "evidence_ids": [item for item in info["evidence_ids"] if item in set(info["allowed"])][
                        :20
                    ],
                    "speculative": True,
                }
            ],
            "relations": [],
            "unverified_guesses": ["transcript missing; topic is speculative"],
        }

    first = excerpts[0]
    last = excerpts[-1]
    text = first.get("text_original") or "课程主题"
    title = text[:40]
    evidence = [
        item
        for item in (info["evidence_ids"] or [row["id"] for row in excerpts] + [row["id"] for row in info["frames"]])
        if item in set(info["allowed"])
    ]
    topics = [
        {
            "id": f"topic-{info['id']}",
            "title": title,
            "goal": f"理解：{text[:80]}",
            "start_seconds": info["start"],
            "end_seconds": info["end"],
            "evidence_ids": evidence[:20],
            "speculative": False,
        }
    ]
    if last is not first and abs(float(last.get("end_seconds", 0)) - float(first.get("start_seconds", 0))) > 1e-6:
        topics[0]["title"] = f"{first.get('text_original', '开始')[:20]} → {last.get('text_original', '结束')[:20]}"
    return {"topics": topics, "relations": [], "unverified_guesses": []}


def _kind_for_text(text: str) -> str:
    lowered = text.lower()
    if any(marker in text or marker in lowered for marker in ("回顾", "复习", "recap")):
        return "recap"
    if any(marker in text or marker in lowered for marker in ("例如", "比如", "example")):
        return "example"
    if any(marker in text or marker in lowered for marker in ("步骤", "首先", "step ", "step")):
        return "procedure"
    if any(marker in text or marker in lowered for marker in ("对比", "versus", "vs")):
        return "comparison"
    if any(marker in text or marker in lowered for marker in ("注意", "警告", "warning")):
        return "warning"
    return "concept"


def _unit_from_excerpt(
    *,
    segment_id: str,
    topic_id: str,
    excerpt: dict[str, Any],
    frames: list[dict[str, Any]],
    allowed: set[str],
    index: int,
    core: tuple[float, float],
) -> dict[str, Any] | None:
    text = excerpt.get("text_original") or "课程内容"
    kind = _kind_for_text(text)
    excerpt_start = float(excerpt.get("start_seconds", core[0]))
    excerpt_end = float(excerpt.get("end_seconds", core[1]))
    if excerpt_end <= core[0] or excerpt_start >= core[1]:
        return None
    start = max(excerpt_start, core[0])
    end = min(excerpt_end, core[1])
    if end <= start:
        return None
    nearby = [
        frame
        for frame in frames
        if start - 15 <= float(frame.get("timestamp_seconds", start)) < end + 15
        and frame["id"] in allowed
    ]
    evidence = [excerpt["id"]] if excerpt.get("id") in allowed else []
    evidence.extend(frame["id"] for frame in nearby if frame["id"] in allowed)
    if kind == "procedure":
        evidence.extend(frame["id"] for frame in frames if frame["id"] in allowed)
    evidence = [item for item in dict.fromkeys(evidence) if item in allowed]
    if not evidence:
        return None
    if text_has_negation(text) and not text_has_negation(text[:400]):
        text = text[:400]
    relations = []
    if kind == "procedure" and len([item for item in evidence if item in {frame["id"] for frame in frames}]) >= 2:
        relations = []
    return {
        "id": f"unit-{segment_id}-{index}",
        "topic_id": topic_id,
        "segment_ids": [segment_id],
        "start_seconds": start,
        "end_seconds": end,
        "kind": kind,
        "claims": [
            {
                "id": f"claim-{segment_id}-{index}",
                "text": text[:400],
                "evidence_ids": evidence[:8],
                "status": "draft",
                "qualifiers": [],
                "modality": "both" if nearby else "audio",
                "provenance": "source",
            }
        ],
        "relations": relations,
        "visual_candidates": [
            {
                "frame_id": frame["id"],
                "relevance": 0.8,
                "legibility": 0.7,
                "selection_reason": "scheduled frame in window",
            }
            for frame in nearby
        ][:8],
        "uncertainty": [],
        "evidence_requests": [],
    }


def synthetic_segment_from_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    payload = payload or {}
    info = _block_from_payload(payload)
    segment_id = str(payload.get("segment_id") or info["id"])
    topic_id = str((payload.get("course_context") or {}).get("topic_id") or f"topic-{segment_id}")
    excerpts = info["excerpts"]
    frames = list(payload.get("frames") or info["frames"])
    allowed = set(payload.get("allowed_evidence_ids") or info["allowed"])
    core = payload.get("core_range") or [info["start"], info["end"]]
    start, end = float(core[0]), float(core[1])
    units: list[dict[str, Any]] = []
    if excerpts:
        for index, excerpt in enumerate(excerpts, start=1):
            built = _unit_from_excerpt(
                segment_id=segment_id,
                topic_id=topic_id,
                excerpt=excerpt,
                frames=frames,
                allowed=allowed,
                index=index,
                core=(start, end),
            )
            if built is not None:
                units.append(built)
        return {"units": units}
    if not frames:
        return {"units": []}
    evidence = [frame["id"] for frame in frames if frame["id"] in allowed]
    if not evidence:
        return {"units": []}
    return {
        "units": [
            {
                "id": f"unit-{segment_id}",
                "topic_id": topic_id,
                "segment_ids": [segment_id],
                "start_seconds": start,
                "end_seconds": end,
                "kind": "concept",
                "claims": [
                    {
                        "id": f"claim-{segment_id}-1",
                        "text": "可见画面",
                        "evidence_ids": evidence[:8],
                        "status": "draft",
                        "qualifiers": [],
                        "modality": "visual",
                        "provenance": "source",
                    }
                ],
                "relations": [],
                "visual_candidates": [],
                "uncertainty": [],
                "evidence_requests": [],
            }
        ]
    }


def synthetic_editor_from_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    payload = payload or {}
    selected = list(payload.get("selected") or payload.get("candidates") or [])
    title = str(payload.get("course_title") or "课程讲义")[:80]
    pages = [
        {
            "id": "intent-cover",
            "type": "cover",
            "layout": None,
            "title": title,
            "claim_ids": [],
            "frame_ids": [],
            "notes": "",
            "selection_reason": "封面",
            "quality_label": "draft",
            "body_points": [],
        }
    ]
    for item in selected:
        pages.append(
            {
                "id": item.get("id") or f"intent-{len(pages):03d}",
                "type": "quiz" if item.get("kind") == "quiz" else "content",
                "layout": item.get("layout") or "text",
                "title": str(item.get("title") or "要点")[:80],
                "claim_ids": list(item.get("claim_ids") or []),
                "frame_ids": list(item.get("frame_ids") or []),
                "notes": item.get("notes") or "",
                "selection_reason": item.get("selection_reason") or "候选",
                "quality_label": "draft",
                "body_points": list(item.get("body_points") or []),
            }
        )
    pages.append(
        {
            "id": "intent-summary",
            "type": "summary",
            "layout": None,
            "title": "本课总结",
            "claim_ids": [claim_id for item in selected for claim_id in item.get("claim_ids") or []][:8],
            "frame_ids": [],
            "notes": "总结复用已选 claim。",
            "selection_reason": "总结",
            "quality_label": "draft",
            "body_points": [str(item.get("title") or "")[:200] for item in selected[:4] if item.get("title")],
        }
    )
    return {"pages": pages, "omissions": list(payload.get("omissions") or [])}


def synthetic_verifier_from_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    payload = payload or {}
    if payload.get("claim"):
        claim = payload["claim"]
        evidence = str(payload.get("evidence_text") or "")
        return {"text": evidence[:400] or claim.get("text"), "evidence_ids": claim.get("evidence_ids") or []}
    return {"verdicts": list(payload.get("draft_verdicts") or [])}


def course_responder(provider: FakeProvider, request: ModelRequest) -> dict[str, Any]:
    payload = provider.last_payload
    if request.role == "outline":
        return synthetic_outline_from_payload(payload)
    if request.role == "segment":
        return synthetic_segment_from_payload(payload)
    if request.role == "editor":
        return synthetic_editor_from_payload(payload)
    if request.role == "verifier":
        return synthetic_verifier_from_payload(payload)
    return {"ok": True}


def fake_course_provider(capabilities: Any) -> FakeProvider:
    return FakeProvider(capabilities, responder=course_responder)

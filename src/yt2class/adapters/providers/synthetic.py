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
    if any(marker in text or marker in lowered for marker in ("回顾", "复习", "recap", "刚才")):
        return "recap"
    if any(marker in text or marker in lowered for marker in ("例如", "比如", "example")):
        return "example"
    if any(marker in text or marker in lowered for marker in ("步骤", "首先", "然后", "step", "1.", "2.")):
        return "procedure"
    if any(marker in text or marker in lowered for marker in ("对比", "不同", "versus", "vs")):
        return "comparison"
    if any(marker in text or marker in lowered for marker in ("注意", "不要", "警告", "warning")):
        return "warning"
    return "concept"


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
    if not excerpts and not frames:
        return {"units": []}

    texts = [row.get("text_original") or "" for row in excerpts]
    joined = " ".join(texts) or "可见画面"
    kind = _kind_for_text(joined)
    evidence = [item for item in (info["evidence_ids"] or [row["id"] for row in excerpts] + [row["id"] for row in frames]) if item in allowed]
    if not evidence:
        return {"units": []}

    claim_text = joined[:400]
    if text_has_negation(joined) and not text_has_negation(claim_text):
        claim_text = joined[:400]
    if text_has_units(joined) and not text_has_units(claim_text):
        claim_text = joined[:400]

    relations: list[dict[str, str]] = []
    if kind == "procedure" and len(frames) >= 2:
        relations.append(
            {
                "from_id": f"claim-{segment_id}-1",
                "to_id": f"claim-{segment_id}-2" if len(excerpts) > 1 else f"unit-{segment_id}",
                "kind": "step_before",
            }
        )

    claims = [
        {
            "id": f"claim-{segment_id}-1",
            "text": claim_text or "课程内容",
            "evidence_ids": evidence[:8],
            "status": "draft",
            "qualifiers": [],
            "modality": "both" if excerpts and frames else ("audio" if excerpts else "visual"),
            "provenance": "source",
        }
    ]
    if kind == "procedure" and len(excerpts) > 1:
        second_evidence = [excerpts[1]["id"]] if excerpts[1]["id"] in allowed else evidence[:1]
        claims.append(
            {
                "id": f"claim-{segment_id}-2",
                "text": excerpts[1].get("text_original") or "下一步",
                "evidence_ids": second_evidence,
                "status": "draft",
                "qualifiers": [],
                "modality": "audio",
                "provenance": "source",
            }
        )

    units.append(
        {
            "id": f"unit-{segment_id}",
            "topic_id": topic_id,
            "segment_ids": [segment_id],
            "start_seconds": start,
            "end_seconds": end,
            "kind": kind,
            "claims": claims,
            "relations": relations if kind == "procedure" and len(frames) >= 2 and len(claims) > 1 else [],
            "visual_candidates": [
                {
                    "frame_id": frame["id"],
                    "relevance": 0.8,
                    "legibility": 0.7,
                    "selection_reason": "scheduled frame in window",
                }
                for frame in frames
                if frame["id"] in allowed
            ][:8],
            "uncertainty": [],
            "evidence_requests": [],
        }
    )
    return {"units": units}


def course_responder(provider: FakeProvider, request: ModelRequest) -> dict[str, Any]:
    payload = provider.last_payload
    if request.role == "outline":
        return synthetic_outline_from_payload(payload)
    if request.role == "segment":
        return synthetic_segment_from_payload(payload)
    return {"ok": True}


def fake_course_provider(capabilities: Any) -> FakeProvider:
    return FakeProvider(capabilities, responder=course_responder)

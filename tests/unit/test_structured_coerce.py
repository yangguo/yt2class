"""Unit tests for model JSON alias/coercion (Ling outline/segment shapes)."""

from __future__ import annotations

from yt2class.stages.structured_coerce import (
    coerce_interval,
    coerce_topic,
    extract_units_list,
)


def test_extract_units_list_accepts_aliases_and_rejects_wrong_type():
    assert extract_units_list({"knowledge_units": [{"id": "u1"}]}) == [{"id": "u1"}]
    assert extract_units_list({"units": {"u1": {"id": "u1"}}}) == [{"id": "u1"}]
    assert extract_units_list({"kind": "concept", "claims": []}) == [
        {"kind": "concept", "claims": []}
    ]
    assert extract_units_list({"units": "nope"}) is None
    assert extract_units_list({"topics": []}) is None


def test_coerce_interval_does_not_invent_or_swap_invalid_times():
    assert coerce_interval({}, fallback_start=1.0, fallback_end=5.0) == (1.0, 5.0)
    assert coerce_interval({"start": 2, "end": 4}, fallback_start=0.0, fallback_end=10.0) == (
        2.0,
        4.0,
    )
    assert coerce_interval({"start_seconds": 5, "end_seconds": 1}, fallback_start=0.0, fallback_end=10.0) is None
    assert coerce_interval({"start": "10:16"}, fallback_start=0.0, fallback_end=10.0) == (0.0, 10.0)


def test_coerce_topic_prefers_student_goal_and_strips_aliases():
    topic = coerce_topic(
        {
            "title": "自动词",
            "teaching_goal": "讲清自动词",
            "student_goal": "能区分自动词",
            "notes": "forbidden extra",
        },
        block_id="block-0001",
        block_start=0.0,
        block_end=20.0,
        index=1,
    )
    assert topic is not None
    assert topic["goal"] == "能区分自动词"
    assert "teaching_goal" not in topic
    assert "notes" not in topic
    assert topic["speculative"] is True
    assert "teaching_goal" not in topic


def test_prompts_list_exact_json_keys():
    from yt2class.stages.llm_util import load_prompt

    outline = load_prompt("outline.md")
    assert '"goal"' in outline
    assert "start_seconds" in outline
    assert "Do not emit `teaching_goal` or `student_goal`" in outline
    segment = load_prompt("segment.md")
    assert '"units"' in segment
    assert "start_seconds" in segment
    assert "allowed_evidence_ids" in segment

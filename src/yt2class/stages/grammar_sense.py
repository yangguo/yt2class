"""Grammar-lesson sense headings (用法一/二/三) for editorial slides."""

from __future__ import annotations

import re

from yt2class.domain.course_map import CourseMap, Topic
from yt2class.domain.knowledge import KnowledgeUnit

_CONNECTIVE_HINTS = ("接续", "连接", "导入", "语法点", "文法点", "名词＋", "名词+")
_RATE_TOPIC_HINTS = ("比例", "単位", "单位", "每", "単価")
_CAUSE_TOPIC_HINTS = ("原因", "理由", "用法1", "用法一")
_ABOUT_TOPIC_HINTS = ("关于", "について", "中止", "用法3", "用法三")

_SENSE_HEADINGS = {
    1: "用法一：原因・理由",
    2: "用法二：比例・単位",
    3: "用法三：～についての中止形（罕用）",
}

_CN_ORDINALS = "一二三四五六七八九十"
_RATE_EXAMPLE_HINTS = ("每", "一匹", "一個", "円", "ポイント", "割", "につき", "500", "1500", "1000")


def is_connective_topic(title: str) -> bool:
    lowered = title.strip().lower()
    if any(hint in title for hint in _CONNECTIVE_HINTS):
        return True
    if lowered in {"cover", "intro", "introduction"}:
        return True
    return False


def usage_topics(course_map: CourseMap | None) -> list[Topic]:
    if course_map is None or not course_map.topics:
        return []
    return [topic for topic in course_map.topics if not is_connective_topic(topic.title)]


def sense_ordinal(topic_id: str, course_map: CourseMap | None) -> int | None:
    for index, topic in enumerate(usage_topics(course_map), start=1):
        if topic.id == topic_id:
            return index
    return None


def sense_heading(ordinal: int) -> str:
    if ordinal in _SENSE_HEADINGS:
        return _SENSE_HEADINGS[ordinal]
    if 1 <= ordinal <= len(_CN_ORDINALS):
        return f"用法{_CN_ORDINALS[ordinal - 1]}"
    return f"用法{ordinal}"


def learner_page_title(
    *,
    topic_id: str,
    course_map: CourseMap | None,
    fallback_title: str,
) -> str:
    ordinal = sense_ordinal(topic_id, course_map)
    if ordinal is not None:
        return sense_heading(ordinal)
    return fallback_title


def summary_bullet_for_sense(ordinal: int, example_text: str) -> str:
    body = example_text.strip()
    heading = sense_heading(ordinal)
    if body.startswith(heading):
        return body[:200]
    if body.startswith("用法"):
        return f"{heading}：{body.split('：', 1)[-1].strip()}"[:200]
    return f"{heading}：{body}"[:200]


def _unit_text(unit: KnowledgeUnit) -> str:
    return " ".join(claim.text for claim in unit.claims)


def _topic_kind(topic: Topic) -> str:
    title = topic.title
    if any(h in title for h in _RATE_TOPIC_HINTS):
        return "rate"
    if any(h in title for h in _ABOUT_TOPIC_HINTS):
        return "about"
    if any(h in title for h in _CAUSE_TOPIC_HINTS):
        return "cause"
    return "other"


def pick_summary_unit(
    topic: Topic,
    knowledge: KnowledgeDocument,
    *,
    course_map: CourseMap | None,
) -> KnowledgeUnit | None:
    units = [unit for unit in knowledge.units if unit.topic_id == topic.id]
    if not units:
        return None
    examples = sorted(
        [unit for unit in units if unit.kind == "example"],
        key=lambda item: (item.start_seconds, item.id),
    )
    concepts = sorted(
        [unit for unit in units if unit.kind == "concept"],
        key=lambda item: (item.start_seconds, item.id),
    )
    kind = _topic_kind(topic)
    if kind == "rate" and examples:
        for unit in examples:
            if any(hint in _unit_text(unit) for hint in _RATE_EXAMPLE_HINTS):
                return unit
    if examples:
        return examples[0]
    if concepts:
        return concepts[0]
    return units[0]


__all__ = [
    "is_connective_topic",
    "learner_page_title",
    "pick_summary_unit",
    "sense_heading",
    "sense_ordinal",
    "summary_bullet_for_sense",
    "usage_topics",
]

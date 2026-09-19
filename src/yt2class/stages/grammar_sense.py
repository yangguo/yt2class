"""Grammar-lesson sense headings (用法一/二/三) for editorial slides."""

from __future__ import annotations

import re

from yt2class.domain.course_map import CourseMap, Topic
from yt2class.domain.knowledge import KnowledgeDocument, KnowledgeUnit

_CONNECTIVE_HINTS = ("接续", "连接", "导入", "语法点", "文法点", "名词＋", "名词+")
_NON_USAGE_TITLE_RE = re.compile(
    r"(?i)\b("
    r"intro(?:duction)?|overview|warm[- ]?up|recap|transition|preview|"
    r"conversation|small\s*talk|opening|closing|wrap[- ]?up|filler|icebreaker"
    r")\b"
)
_NON_USAGE_CN_HINTS = ("闲聊", "开场", "寒暄", "过渡", "导入语", "片头", "片尾")

_RATE_TOPIC_HINTS = (
    "比例",
    "単位",
    "单位",
    "每",
    "単価",
    "用法2",
    "用法二",
    "usage2",
    "usage 2",
    "rate",
    "proportion",
    "proportional",
    "per item",
)
_CAUSE_TOPIC_HINTS = (
    "原因",
    "理由",
    "用法1",
    "用法一",
    "usage1",
    "usage 1",
    "reason",
    "cause",
)
_ABOUT_TOPIC_HINTS = (
    "关于",
    "について",
    "中止",
    "用法3",
    "用法三",
    "usage3",
    "usage 3",
)

_SENSE_HEADINGS = {
    1: "用法一：原因・理由",
    2: "用法二：比例・単位",
    3: "用法三：～についての中止形（罕用）",
}

_KIND_TO_ORDINAL = {"cause": 1, "rate": 2, "about": 3}
_ORDINAL_TO_KIND = {1: "cause", 2: "rate", 3: "about"}

_CN_ORDINALS = "一二三四五六七八九十"
_USAGE_NUM_RE = re.compile(r"用法\s*([123一二三])")
_RATE_EXAMPLE_HINTS = ("每", "一匹", "一個", "円", "ポイント", "割", "500", "1500", "1000")
_RATE_EXAMPLE_RE = re.compile(
    r"一[個匹本枚]につき|[0-9０-９]+円|ごとに|每[一個人匹本]"
)


def _text_signals_rate(text: str) -> bool:
    if any(hint in text for hint in _RATE_EXAMPLE_HINTS):
        return True
    return _RATE_EXAMPLE_RE.search(text) is not None


def is_connective_topic(title: str) -> bool:
    text = title.strip()
    lowered = text.lower()
    if any(hint in text for hint in _CONNECTIVE_HINTS):
        return True
    if any(hint in text for hint in _NON_USAGE_CN_HINTS):
        return True
    if _NON_USAGE_TITLE_RE.search(text):
        return True
    if lowered in {"cover", "intro", "introduction", "overview"}:
        return True
    return False


def _topic_kind(topic: Topic) -> str:
    title = topic.title
    lowered = title.lower()
    if any(h in title for h in _RATE_TOPIC_HINTS) or any(
        h in lowered for h in ("usage 2", "usage2", "rate", "proportion", "proportional")
    ):
        return "rate"
    if any(h in title for h in _ABOUT_TOPIC_HINTS) or any(
        h in lowered for h in ("usage 3", "usage3", "about", "ni tsuite")
    ):
        return "about"
    if any(h in title for h in _CAUSE_TOPIC_HINTS) or any(
        h in lowered for h in ("usage 1", "usage1", "reason", "cause")
    ):
        return "cause"
    match = _USAGE_NUM_RE.search(title)
    if match:
        digit = match.group(1)
        if digit in {"1", "一"}:
            return "cause"
        if digit in {"2", "二"}:
            return "rate"
        if digit in {"3", "三"}:
            return "about"
    return "other"


def is_grammar_usage_topic(
    topic: Topic,
    knowledge: KnowledgeDocument | None = None,
) -> bool:
    if is_connective_topic(topic.title):
        return False
    return topic_kind(topic, knowledge) in _KIND_TO_ORDINAL


def usage_topics(
    course_map: CourseMap | None,
    *,
    knowledge: KnowledgeDocument | None = None,
) -> list[Topic]:
    if course_map is None or not course_map.topics:
        return []
    return [
        topic
        for topic in course_map.topics
        if is_grammar_usage_topic(topic, knowledge)
    ]


def sense_topic_ids(
    course_map: CourseMap | None,
    knowledge: KnowledgeDocument,
) -> list[str]:
    """Topic ids for ordinals 1..3 that have knowledge units (fixed sense order)."""

    ids: list[str] = []
    for ordinal in (1, 2, 3):
        topic = topic_for_sense_ordinal(course_map, ordinal, knowledge=knowledge)
        if topic is None:
            continue
        if any(unit.topic_id == topic.id for unit in knowledge.units):
            ids.append(topic.id)
    return ids


def _infer_topic_kind_from_knowledge(topic_id: str, knowledge: KnowledgeDocument) -> str:
    units = [unit for unit in knowledge.units if unit.topic_id == topic_id]
    if not units:
        return "other"
    rate = about = cause = 0
    for unit in units:
        text = _unit_text(unit)
        if "について" in text:
            about += 4
        if _text_signals_rate(text):
            rate += 4
        if re.search(r"単価|比例", text):
            rate += 2
        if "につき" in text and "について" not in text and not _text_signals_rate(text):
            cause += 3
    best = max(((rate, "rate"), (about, "about"), (cause, "cause")), key=lambda item: item[0])
    if best[0] <= 0:
        return "other"
    return best[1]


def topic_kind(
    topic: Topic,
    knowledge: KnowledgeDocument | None = None,
) -> str:
    kind = _topic_kind(topic)
    if kind != "other":
        return kind
    if knowledge is not None:
        return _infer_topic_kind_from_knowledge(topic.id, knowledge)
    return "other"


def topic_for_sense_ordinal(
    course_map: CourseMap | None,
    ordinal: int,
    *,
    knowledge: KnowledgeDocument | None = None,
) -> Topic | None:
    if course_map is None or not course_map.topics or ordinal not in _ORDINAL_TO_KIND:
        return None
    want = _ORDINAL_TO_KIND[ordinal]
    for topic in course_map.topics:
        if is_connective_topic(topic.title):
            continue
        if topic_kind(topic, knowledge) == want:
            return topic
    return None


def sense_ordinal(
    topic_id: str,
    course_map: CourseMap | None,
    *,
    knowledge: KnowledgeDocument | None = None,
) -> int | None:
    if course_map is None or not course_map.topics:
        return None
    topic = next((item for item in course_map.topics if item.id == topic_id), None)
    if topic is None or is_connective_topic(topic.title):
        return None
    kind = topic_kind(topic, knowledge)
    if kind in _KIND_TO_ORDINAL:
        return _KIND_TO_ORDINAL[kind]
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
    knowledge: KnowledgeDocument | None = None,
) -> str:
    ordinal = sense_ordinal(topic_id, course_map, knowledge=knowledge)
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
    kind = topic_kind(topic, knowledge)
    if kind == "rate":
        for unit in examples:
            if _text_signals_rate(_unit_text(unit)):
                return unit
        for unit in concepts:
            if _text_signals_rate(_unit_text(unit)):
                return unit
        for unit in units:
            if _text_signals_rate(_unit_text(unit)):
                return unit
    if examples:
        return examples[0]
    if concepts:
        return concepts[0]
    return units[0]


def pick_summary_unit_for_topic(
    topic: Topic,
    knowledge: KnowledgeDocument,
    *,
    course_map: CourseMap | None,
) -> KnowledgeUnit | None:
    """Best learner-safe unit for summary, trying alternates if the first is meta-only."""

    units = [unit for unit in knowledge.units if unit.topic_id == topic.id]
    if not units:
        return None
    ranked = sorted(
        units,
        key=lambda item: (
            0 if item.kind == "example" else 1 if item.kind == "concept" else 2,
            item.start_seconds,
            item.id,
        ),
    )
    primary = pick_summary_unit(topic, knowledge, course_map=course_map)
    if primary is not None:
        ranked = [primary] + [unit for unit in ranked if unit.id != primary.id]
    from yt2class.stages.student_copy import contains_student_meta, sanitize_student_copy

    for unit in ranked:
        if not unit.claims:
            continue
        text = sanitize_student_copy(unit.claims[0].text)
        if not text.strip() or contains_student_meta(text):
            continue
        return unit
    return primary


__all__ = [
    "is_connective_topic",
    "is_grammar_usage_topic",
    "learner_page_title",
    "pick_summary_unit",
    "pick_summary_unit_for_topic",
    "sense_heading",
    "sense_ordinal",
    "sense_topic_ids",
    "summary_bullet_for_sense",
    "topic_for_sense_ordinal",
    "topic_kind",
    "usage_topics",
]

"""Claim verifier: structured fact checks, one repair, then formalize the deck."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from threading import Event
from typing import Any, Iterable

from yt2class.adapters.providers.base import Provider
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
    load_prompt,
    model_request,
    text_has_negation,
)
from yt2class.stages.reduce_knowledge import normalize_concept, polarity, strip_negation

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
    "grounding",
}
ANTONYM_PAIRS = (
    ("打开", "关闭"),
    ("开启", "关闭"),
    ("开通", "关闭"),
    ("开始", "停止"),
    ("允许", "禁止"),
    ("进入", "离开"),
    ("前进", "后退"),
    ("正向", "反向"),
    ("open", "closed"),
    ("open", "close"),
    ("opened", "closed"),
    ("start", "stop"),
    ("on", "off"),
)
# Comparative/scalar predicates are normalized into an up/down polarity instead of
# being matched against a finite antonym list: a signed morpheme carries the
# direction, so unlisted compounds (变多/变少, 变快/变慢, 偏高/偏低, 加速/减速, …)
# normalize too. A neutral morpheme leaves the direction to its partner.
SCALAR_UP_PREFIX = "增升提加上涨扩"
SCALAR_DOWN_PREFIX = "减降下跌缩落"
SCALAR_NEUTRAL_PREFIX = "变更越偏最"
SCALAR_UP_ROOT = "多大高快强长深厚宽远重热满早亮升涨增加"
SCALAR_DOWN_ROOT = "少小低慢弱短浅薄窄近轻冷空晚暗降跌减"
SCALAR_NEUTRAL_ROOT = "速温压幅量额率距"
SCALAR_DIRECTION = {
    **{char: "up" for char in f"{SCALAR_UP_PREFIX}{SCALAR_UP_ROOT}"},
    **{char: "down" for char in f"{SCALAR_DOWN_PREFIX}{SCALAR_DOWN_ROOT}"},
}
SCALAR_RE = re.compile(
    rf"(?P<prefix>变得|越来越|[{SCALAR_UP_PREFIX}{SCALAR_DOWN_PREFIX}{SCALAR_NEUTRAL_PREFIX}])"
    rf"(?P<root>[{SCALAR_UP_ROOT}{SCALAR_DOWN_ROOT}{SCALAR_NEUTRAL_ROOT}])"
    rf"|(?P<compared>[{SCALAR_UP_ROOT}{SCALAR_DOWN_ROOT}])(?=于)"
)
SCALAR_WORDS = {
    "up": (
        "increase",
        "increases",
        "increased",
        "increasing",
        "rise",
        "rises",
        "rising",
        "grow",
        "grows",
        "growing",
        "higher",
        "larger",
        "greater",
        "longer",
        "faster",
        "stronger",
        "more",
    ),
    "down": (
        "decrease",
        "decreases",
        "decreased",
        "decreasing",
        "reduce",
        "reduces",
        "reduced",
        "fall",
        "falls",
        "falling",
        "drop",
        "drops",
        "shrink",
        "shrinks",
        "lower",
        "smaller",
        "shorter",
        "slower",
        "weaker",
        "less",
        "fewer",
    ),
}
SCALAR_WORD_RE = re.compile(
    r"\b(" + "|".join(sorted({word for words in SCALAR_WORDS.values() for word in words})) + r")\b",
    re.I,
)
SCALAR_WORD_DIRECTION = {
    word: direction for direction, words in SCALAR_WORDS.items() for word in words
}
HOW_MUCH_RE = re.compile(r"\bhow\s+$", re.I)
# Sentence-final modal particles and a truncated A-not-A turn a predicate into a
# question rather than an assertion. Closed function-word class, unlike connectives.
SOFT_PARTICLES = ("吗", "呢", "吧", "啊", "呀", "嘛", "么", "？", "?")
TRUNCATED_QUESTION_NEGATORS = ("不", "没", "未")
PARTICLE_SKIP = "了的是着过呢啊 \t"
# Epistemic modals leave an outcome open even with no opposite polarity in sight.
EPISTEMIC_MARKERS = (
    "是否",
    "会不会",
    "是不是",
    "可能",
    "也许",
    "大概",
    "未必",
    "尚未",
    "不确定",
    "未确定",
    "不清楚",
    "待定",
    "多少",
    "whether",
    "maybe",
    "perhaps",
    "unclear",
    "unknown",
    "undetermined",
    "how much",
    "how many",
)
CLAUSE_SPLIT_RE = re.compile(r"[。！!？?；;\n]")
QUALITY_PREFIXES = ("[DRAFT]", "[EVIDENCE-ONLY]")


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


FUNCTION_CHARS = re.compile(r"[是的了與与和把在为請请看先再又就也与及或]")


def content_tokens(text: str) -> set[str]:
    """CJK bigrams plus latin/numeric tokens. Function words are stripped."""

    cleaned = FUNCTION_CHARS.sub("", strip_negation(text).lower())
    tokens = {item.lower() for item in NUMBER_RE.findall(cleaned)}
    tokens.update(item.lower() for item in re.findall(r"[a-zA-Z]{2,}", cleaned))
    for run in re.findall(r"[\u4e00-\u9fff\u3040-\u30ff]+", cleaned):
        if len(run) == 1:
            tokens.add(run)
        else:
            tokens.update(run[index : index + 2] for index in range(len(run) - 1))
    tokens.discard("")
    return tokens


def needed_overlap(tokens: set[str], *, practice: bool = False) -> int:
    if practice or len(tokens) <= 2:
        return 1
    return max(2, (len(tokens) + 2) // 3)


def _has_term(text: str, term: str) -> bool:
    if term.isascii():
        return re.search(rf"\b{re.escape(term)}\b", text, flags=re.I) is not None
    return term in text


@dataclass(frozen=True)
class ScalarTerm:
    text: str
    direction: str
    start: int
    end: int


def _scalar_direction(match: re.Match[str]) -> str | None:
    """Direction of one comparative predicate: the signed morpheme wins."""

    if compared := match.group("compared"):
        return SCALAR_DIRECTION.get(compared)
    prefix, root = match.group("prefix"), match.group("root")
    return SCALAR_DIRECTION.get(prefix[0]) or SCALAR_DIRECTION.get(root)


def _is_measure_form(text: str, match: re.Match[str], direction: str) -> bool:
    """变多少 / 变快慢 ask how much something changed; they carry no direction."""

    tail = text[match.end() : match.end() + 1]
    return bool(tail) and SCALAR_DIRECTION.get(tail) not in {None, direction}


def scalar_terms(text: str) -> list[ScalarTerm]:
    """Comparative/scalar predicates in ``text`` with their up/down polarity and span."""

    found: list[ScalarTerm] = []
    for match in SCALAR_RE.finditer(text):
        direction = _scalar_direction(match)
        if direction is None or _is_measure_form(text, match, direction):
            continue
        found.append(ScalarTerm(match.group(0), direction, match.start(), match.end()))
    for match in SCALAR_WORD_RE.finditer(text):
        if HOW_MUCH_RE.search(text[max(0, match.start() - 8) : match.start()]):
            continue
        found.append(
            ScalarTerm(
                match.group(0),
                SCALAR_WORD_DIRECTION[match.group(0).lower()],
                match.start(),
                match.end(),
            )
        )
    return sorted(found, key=lambda item: item.start)


def _clause_around(text: str, term: ScalarTerm) -> str:
    start, end = 0, len(text)
    for match in CLAUSE_SPLIT_RE.finditer(text):
        if match.end() <= term.start:
            start = match.end()
        elif match.start() >= term.end:
            end = match.start()
            break
    return text[start:end]


def _is_epistemic(text: str, term: ScalarTerm) -> bool:
    clause = _clause_around(text, term).lower()
    return any(marker.lower() in clause for marker in EPISTEMIC_MARKERS)


def _is_softened(text: str, term: ScalarTerm) -> bool:
    """变多吧 / 变多不 / 变多不多 offer the direction without asserting it."""

    tail = text[term.end :].lstrip(PARTICLE_SKIP)
    if tail[:1] in SOFT_PARTICLES:
        return True
    if tail[:1] not in TRUNCATED_QUESTION_NEGATORS:
        return False
    after = tail[1:2]
    return not after or after == term.text[-1:] or CLAUSE_SPLIT_RE.match(after) is not None


def settled_scalar_terms(text: str) -> list[ScalarTerm]:
    """Scalar predicates ``text`` asserts on its own terms.

    Drops predicates softened by a sentence-final particle or a truncated A-not-A
    question, and predicates whose clause is explicitly epistemic. Whether the
    evidence settles a direction *for a given claim* is decided separately by
    :func:`affirmed_directions`, which needs the claim's own skeleton.
    """

    return [
        term
        for term in scalar_terms(text)
        if not _is_softened(text, term) and not _is_epistemic(text, term)
    ]


def scalar_directions(text: str) -> set[str]:
    return {term.direction for term in settled_scalar_terms(text)}


def _strip_scalars(text: str) -> str:
    stripped = text
    for term in reversed(scalar_terms(text)):
        stripped = f"{stripped[: term.start]} {stripped[term.end :]}"
    return stripped


def predicate_skeleton(text: str) -> set[str]:
    """Subject/object tokens left once polarity and scalar predicates are removed."""

    return content_tokens(_strip_scalars(text))


def _same_skeleton(left: set[str], right: set[str]) -> bool:
    if not left or not right:
        return not left and not right
    shared = left & right
    return len(shared) >= max(1, min(len(left), len(right)) // 2)


def _subject_slot(text: str, term: ScalarTerm, terms: list[ScalarTerm]) -> set[str]:
    """Content tokens between the previous predicate and this one: its subject slot."""

    previous = max((item.end for item in terms if item.end <= term.start), default=0)
    return content_tokens(text[previous : term.start])


def _claim_dominates(claim_skeleton: set[str], evidence_skeleton: set[str]) -> bool:
    """True when the claim accounts for most of the evidence's subject material.

    A predicate appended to the claim's own wording has no subject of its own to
    attach to, so it speaks about the claim. This replaces asking which connective
    joins them: 变多却下降了, 变多然后下降了 and 变多结果下降了 are all dominated,
    while 加热使温度升高并且压力降低 says far more than a 温度升高 claim does.
    """

    if not evidence_skeleton:
        return True
    return len(claim_skeleton & evidence_skeleton) * 2 >= len(evidence_skeleton)


def _adjacent_root_directions(text: str, term: ScalarTerm, terms: list[ScalarTerm]) -> set[str]:
    """Lone roots beside a predicate: 变多或少 offers 少 without spelling out 变少."""

    covered = {index for item in terms for index in range(item.start, item.end)}
    window = range(max(0, term.start - 3), min(len(text), term.end + 3))
    return {
        direction
        for index in window
        if index not in covered and (direction := SCALAR_DIRECTION.get(text[index]))
    }


def unsettled_directions(claim_text: str, evidence_text: str) -> set[str]:
    """Polarities the evidence leaves open for the claim's own proposition.

    Both an up and a down polarity over the claim's proposition means the evidence
    never settles either one, whatever connective or punctuation sits between them.
    A predicate counts as speaking about the claim when the claim dominates the
    evidence's subject material, or when its own subject slot overlaps the claim's
    skeleton — so a genuinely different subject keeps its own polarity.
    """

    terms = scalar_terms(evidence_text)
    if not terms:
        return set()
    claim_directions = {term.direction for term in scalar_terms(claim_text)}
    if {"up", "down"} <= claim_directions:
        # The claim reports both directions itself, so it picks no side to smuggle.
        return set()
    claim_skeleton = predicate_skeleton(claim_text)
    dominates = _claim_dominates(claim_skeleton, predicate_skeleton(evidence_text))
    about_claim: set[str] = set()
    for term in terms:
        if not dominates and not (_subject_slot(evidence_text, term, terms) & claim_skeleton):
            continue
        about_claim.add(term.direction)
        about_claim |= _adjacent_root_directions(evidence_text, term, terms)
    return about_claim if {"up", "down"} <= about_claim else set()


def affirmed_directions(claim_text: str, evidence_text: str) -> set[str]:
    """Polarities the evidence actually settles for ``claim_text``."""

    return scalar_directions(evidence_text) - unsettled_directions(claim_text, evidence_text)


def direction_conflicts(left: str, right: str) -> list[str]:
    """One-sided opposite polarity on the same subject/object skeleton."""

    left_dirs = scalar_directions(left)
    right_dirs = affirmed_directions(left, right)
    if len(left_dirs) != 1 or len(right_dirs) != 1 or left_dirs == right_dirs:
        return []
    if not _same_skeleton(predicate_skeleton(left), predicate_skeleton(right)):
        return []
    return [f"direction:{next(iter(left_dirs))}/{next(iter(right_dirs))}"]


def unaffirmed_predicates(text: str, evidence_text: str) -> list[str]:
    """Scalar predicates in ``text`` the evidence does not settle in the same direction.

    Substring presence is not affirmation: 变多 occurs inside 变多少, and evidence that
    only raises the direction as a question, an alternative, or a correction never
    affirms it.
    """

    affirmed = affirmed_directions(text, evidence_text)
    return list(
        dict.fromkeys(
            term.text for term in scalar_terms(text) if term.direction not in affirmed
        )
    )


def predicate_conflicts(left: str, right: str) -> list[str]:
    """Opposite predicates, polarity, or quantities. Bag-of-tokens overlap is not enough."""

    if not left.strip() or not right.strip():
        return []
    found: list[str] = []
    for first, second in ANTONYM_PAIRS:
        left_first, left_second = _has_term(left, first), _has_term(left, second)
        right_first, right_second = _has_term(right, first), _has_term(right, second)
        if (left_first and not left_second) and right_second:
            found.append(f"{first}/{second}")
        elif (left_second and not left_first) and right_first:
            found.append(f"{second}/{first}")
    found.extend(direction_conflicts(left, right))
    left_numbers = NUMBER_RE.findall(left)
    right_numbers = set(NUMBER_RE.findall(right))
    if left_numbers and any(item not in right_numbers for item in left_numbers):
        found.append("number")
    if (
        normalize_concept(left)
        and normalize_concept(left) == normalize_concept(right)
        and polarity(left) != polarity(right)
    ):
        found.append("negation")
    return list(dict.fromkeys(found))


def copy_is_affirmed(text: str, evidence_text: str, *, practice: bool = False) -> bool:
    """True when copy is evidence-grounded and does not contradict the evidence."""

    stripped = text.strip()
    if not stripped:
        return True
    if predicate_conflicts(stripped, evidence_text):
        return False
    if unaffirmed_predicates(stripped, evidence_text):
        return False
    tokens = content_tokens(stripped)
    if not tokens:
        return True
    covered = tokens & content_tokens(evidence_text)
    if practice:
        return len(covered) >= needed_overlap(tokens, practice=True)
    return covered == tokens


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


def check_grounding(claim: KnowledgeClaim, index: dict[str, str]) -> CheckResult:
    tokens = content_tokens(claim.text)
    if not tokens:
        return CheckResult(
            kind="grounding",
            passed=False,
            note="claim has no grounded content",
            verdict="insufficient",
        )
    supporting: list[str] = []
    union: set[str] = set()
    excerpts: list[tuple[str, str]] = []
    contradicting: list[str] = []
    for evidence_id in claim.evidence_ids:
        excerpt = (index.get(evidence_id) or "").strip()
        if not excerpt:
            continue
        excerpts.append((evidence_id, excerpt))
        excerpt_tokens = content_tokens(excerpt)
        union |= excerpt_tokens
        if predicate_conflicts(claim.text, excerpt):
            contradicting.append(evidence_id)
            continue
        if tokens & excerpt_tokens:
            supporting.append(evidence_id)
    joined = " ".join(text for _, text in excerpts)
    if contradicting or predicate_conflicts(claim.text, joined):
        return CheckResult(
            kind="grounding",
            passed=False,
            note="cited evidence contradicts the claim predicate",
            verdict="contradicted",
            contradicting_ids=list(dict.fromkeys(contradicting or [item for item, _ in excerpts])),
        )
    covered = tokens & union
    practice = claim.provenance == "generated-practice"
    # Fail closed: a source claim is only affirmed when the evidence carries all of
    # its content, so an unverified predicate/quantity swap can never read supported.
    if practice:
        affirmed = len(covered) >= needed_overlap(tokens, practice=True)
        unaffirmed: list[str] = []
    else:
        unaffirmed = sorted(tokens - covered) + unaffirmed_predicates(claim.text, joined)
        affirmed = not unaffirmed
    if not supporting or not affirmed:
        detail = f" (unaffirmed {unaffirmed[:8]})" if unaffirmed else ""
        return CheckResult(
            kind="grounding",
            passed=False,
            note=f"cited evidence does not affirm the claim{detail}"[:240],
            verdict="insufficient",
        )
    return CheckResult(
        kind="grounding",
        passed=True,
        note="evidence excerpts support the claim",
        supporting_ids=supporting,
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
        check_grounding(claim, index),
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
    transcript: TranscriptDocument | None = None,
    visual: VisualCatalogue | None = None,
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
                page, knowledge=knowledge, transcript=transcript, visual=visual
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
) -> bool:
    """True when page title/notes/body_points are affirmed by the page's evidence."""

    evidence_text = page_evidence_text(
        page, knowledge=knowledge, transcript=transcript, visual=visual
    )
    if not content_tokens(evidence_text):
        return False
    texts = [page.title, *page.body_points, notes_without_quality(page.notes)]
    practice = _is_practice(knowledge, list(page.claim_ids))
    return all(copy_is_affirmed(text, evidence_text, practice=practice) for text in texts)


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
    repaired_ids: list[str] = list(existing.repaired_claim_ids) if existing is not None else []
    check_by_claim: dict[str, list[CheckResult]] = {}
    verdicts: dict[str, ClaimVerdict] = {}

    if existing is not None:
        for item in existing.verdicts:
            if item.claim_id not in target_ids:
                verdicts[item.claim_id] = item
        pending.extend(item for item in existing.pending_review if item not in target_ids)
        removed.extend(item for item in existing.removed_from_formal if item not in target_ids)

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

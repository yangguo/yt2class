"""EditorialPlan: page intents. Must not contain source paths or hashes."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from yt2class.domain.common import Identifier, StrictModel, unique_ids

PageType = Literal["cover", "content", "summary", "quiz"]
PageLayout = Literal["image-text", "text", "comparison", "sequence"]
DeckOrder = Literal["chronological", "teaching"]
PageQualityLabel = Literal["verified", "draft", "evidence-only"]

QUALITY_NOTE_MARKERS: dict[PageQualityLabel, str] = {
    "draft": "[DRAFT] 待核对，不能当作已核验结论。",
    "evidence-only": "[EVIDENCE-ONLY] 仅证据索引，不是语义摘要，不能当作已核验结论。",
}


class PageIntent(StrictModel):
    id: Identifier
    type: PageType
    layout: PageLayout | None = None
    title: str = Field(min_length=1, max_length=80)
    claim_ids: list[Identifier] = Field(default_factory=list, max_length=8)
    frame_ids: list[Identifier] = Field(default_factory=list, max_length=3)
    notes: str = Field(default="", max_length=4000)
    selection_reason: str = Field(min_length=1, max_length=400)
    locked: bool = False
    quality_label: PageQualityLabel = "draft"
    body_points: list[str] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def check_page_copy(self) -> Self:
        for point in self.body_points:
            if not point or len(point) > 200:
                raise ValueError("body_points items must be 1-200 characters")
        if self.quality_label == "verified" and (
            self.notes.startswith("[DRAFT]") or self.notes.startswith("[EVIDENCE-ONLY]")
        ):
            raise ValueError("verified pages cannot carry draft or evidence-only markers")
        return self


class Omission(StrictModel):
    topic_id: Identifier | None = None
    claim_id: Identifier | None = None
    reason: str = Field(min_length=1, max_length=400)


class EditorialPlan(StrictModel):
    schema_version: Literal["1.0"]
    source_id: Identifier
    target_pages: int = Field(ge=4, le=100)
    max_pages: int = Field(ge=4, le=100)
    order: DeckOrder = "chronological"
    pages: list[PageIntent] = Field(min_length=1)
    omissions: list[Omission] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_pages(self) -> Self:
        if self.max_pages < self.target_pages:
            raise ValueError("max_pages must be >= target_pages")
        unique_ids(self.pages, label="editorial page")
        if len(self.pages) > self.max_pages:
            raise ValueError("editorial pages exceed max_pages")
        if any(page.quality_label == "verified" for page in self.pages) and any(
            page.quality_label in {"draft", "evidence-only"} for page in self.pages
        ):
            # Mixed decks are allowed only when unverified pages stay visibly marked.
            unmarked = [
                page.id
                for page in self.pages
                if page.quality_label in {"draft", "evidence-only"}
                and QUALITY_NOTE_MARKERS[page.quality_label].split("，")[0] not in page.notes
                and QUALITY_NOTE_MARKERS[page.quality_label] not in page.notes
            ]
            if unmarked:
                raise ValueError(
                    f"draft/evidence-only pages {unmarked} must be visibly marked; "
                    "they cannot be disguised as verified"
                )
        return self


def visible_quality_notes(label: PageQualityLabel, notes: str) -> str:
    """Prefix speaker notes so draft/evidence-only copy cannot look verified."""

    if label == "verified":
        return notes
    marker = QUALITY_NOTE_MARKERS[label]
    if marker in notes or notes.startswith(marker.split("，")[0]):
        return notes
    return f"{marker} {notes}".strip()


def relabel_page(page: PageIntent, label: PageQualityLabel) -> PageIntent:
    notes = page.notes
    for marker in QUALITY_NOTE_MARKERS.values():
        if notes.startswith(marker):
            notes = notes[len(marker) :].strip()
    if label != "verified":
        notes = visible_quality_notes(label, notes)
    return page.model_copy(update={"quality_label": label, "notes": notes})

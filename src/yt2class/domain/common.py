"""Shared strict types for 3.0 domain documents.

JSON Schema can express types, enums, string lengths, and ``additionalProperties``.
It cannot express half-open interval pairing, unique object-property IDs, or
cross-document reference closure — those errors are raised only by Pydantic.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

Identifier = Annotated[str, Field(min_length=1, max_length=100)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Seconds = Annotated[float, Field(ge=0, allow_inf_nan=False)]
PositiveSeconds = Annotated[float, Field(gt=0, allow_inf_nan=False)]
UnitInterval = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
# Portable paths relative to run root; binder tests later check resolved symlinks.
RelativePath = Annotated[
    str,
    Field(min_length=1, pattern=r"^(?!/)(?!.*\\)(?!.*:)(?!.*(?:^|/)\.\.(?:/|$)).+$"),
]

JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"

HALF_OPEN_INTERVAL_MESSAGE = (
    "must be a half-open interval [start, end) with start < end; "
    "JSON Schema cannot express this cross-field constraint"
)
UNIQUE_ID_MESSAGE = "JSON Schema cannot enforce unique object-property ids"
FINITE_NUMBER_MESSAGE = "numbers must be finite; JSON Schema has no NaN/Infinity tokens"
EXTRA_FIELD_MESSAGE = "extra fields are rejected (additionalProperties=false / extra=forbid)"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, regex_engine="python-re")


def validate_half_open(start: float, end: float, *, label: str = "interval") -> None:
    if not start < end:
        raise ValueError(f"{label} {HALF_OPEN_INTERVAL_MESSAGE}")


def unique_ids(items: Iterable[Any], *, attr: str = "id", label: str) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for item in items:
        key = getattr(item, attr)
        if key in found:
            raise ValueError(f"duplicate {label} id {key!r}; {UNIQUE_ID_MESSAGE}")
        found[key] = item
    return found


def published_schema(model: type[BaseModel], *, description: str) -> dict[str, Any]:
    """Draft 2020-12 schema derived from a Pydantic model, plus published metadata."""

    schema = model.model_json_schema()
    schema["$schema"] = JSON_SCHEMA_DIALECT
    schema["description"] = description
    return schema

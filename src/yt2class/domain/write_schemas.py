"""Write Draft 2020-12 schemas next to the repository root ``schemas/`` directory."""

from __future__ import annotations

import json
from pathlib import Path

from yt2class.domain.common import published_schema
from yt2class.domain.registry import SCHEMA_DESCRIPTIONS, SCHEMA_MODELS


def write_schemas(root: Path | None = None) -> Path:
    target = (root or Path(__file__).resolve().parents[3]) / "schemas"
    target.mkdir(parents=True, exist_ok=True)
    for filename, model in SCHEMA_MODELS.items():
        payload = published_schema(model, description=SCHEMA_DESCRIPTIONS[filename])
        (target / filename).write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    return target


if __name__ == "__main__":
    print(write_schemas())

"""Data-model service: load and write a project's data model (the named schemas
under examples/<name>/schemas/). This module is the sole writer of schemas/ —
generation hands its validated result here, and readers that need a validated
SchemaLibrary (not raw dicts) load through here."""
from __future__ import annotations

import json
from pathlib import Path

from app.models import parse_schema_library
from app.models.named_schemas import SchemaLibrary
from app.services import workspace


def load_data_model(project_dir: Path) -> SchemaLibrary | None:
    """The project's data model as a validated SchemaLibrary; None when absent."""
    schemas = workspace.load_schemas(project_dir)
    if not schemas:
        return None
    # Strip the loader's bookkeeping keys (_filename/…) before the model validates.
    return parse_schema_library(
        [{k: v for k, v in s.items() if not k.startswith("_")} for s in schemas]
    )


def write_data_model(project_dir: Path, library: SchemaLibrary) -> None:
    """Replace schemas/ with the given data model — clear stale files a shrinking
    re-generation would leave, then write one NN_<name>.json per schema. The library
    is already validated by the caller, so this only writes."""
    schemas_dir = project_dir / "schemas"
    schemas_dir.mkdir(parents=True, exist_ok=True)
    for stale in schemas_dir.glob("*.json"):
        stale.unlink()
    for index, schema in enumerate(library.schemas, start=1):
        payload = schema.model_dump(mode="json", exclude_none=True)
        path = schemas_dir / f"{index:02d}_{schema.name}.json"
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

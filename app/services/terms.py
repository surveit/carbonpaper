"""Terms service: a project's nouns (the named schemas under <project>/schemas/) and its
verbs (<project>/verbs.json). This module is the sole writer of both — generation hands
its validated result here, and readers that need validated models, not raw dicts, load
through here."""
from __future__ import annotations

import json
from pathlib import Path

from app.models import parse_schema_library
from app.models.named_schemas import SchemaLibrary
from app.models.terms import Terms, Verb, parse_verbs
from app.services import workspace

_VERBS_FILENAME = "verbs.json"


def load_terms(project_dir: Path) -> Terms:
    """Raises where a word means two things — reading the halves together is what catches it."""
    return Terms(
        nouns=load_data_model(project_dir) or SchemaLibrary(schemas=[]),
        verbs=load_verbs(project_dir),
    )


def load_data_model(project_dir: Path) -> SchemaLibrary | None:
    schemas = workspace.load_schemas(project_dir)
    if not schemas:
        return None
    # Strip the loader's bookkeeping keys (_filename/…) before the model validates.
    return parse_schema_library(
        [{k: v for k, v in s.items() if not k.startswith("_")} for s in schemas]
    )


def write_data_model(project_dir: Path, library: SchemaLibrary) -> None:
    """Replaces schemas/ wholesale: every existing *.json is deleted first."""
    schemas_dir = project_dir / "schemas"
    schemas_dir.mkdir(parents=True, exist_ok=True)
    for stale in schemas_dir.glob("*.json"):
        stale.unlink()
    for index, schema in enumerate(library.schemas, start=1):
        payload = schema.model_dump(mode="json", exclude_none=True)
        path = schemas_dir / f"{index:02d}_{schema.name}.json"
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def load_verbs(project_dir: Path) -> list[Verb]:
    path = Path(project_dir) / _VERBS_FILENAME
    if not path.is_file():
        return []
    return parse_verbs(path.read_text(encoding="utf-8"))


def write_verbs(project_dir: Path, verbs: list[Verb]) -> None:
    """No verbs is no file: absence is how a project without them is stored."""
    path = Path(project_dir) / _VERBS_FILENAME
    if not verbs:
        path.unlink(missing_ok=True)
        return
    payload = [verb.model_dump(mode="json") for verb in verbs]
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

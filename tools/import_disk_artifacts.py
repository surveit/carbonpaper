"""Bring a project working copy's on-disk artifacts into the document store.

Alembic reaches the store's JSON payloads; the artifacts still under
`<project>/schemas/` were never in it. This is their one-way path in: read each
project directory, validate what it holds, and save it as the document today's
code reads.

Refuses a project it cannot determine rather than storing a guess — a refusal
names the project and why, and never holds back the projects that did import.

Usage:  python -m tools.import_disk_artifacts [--apply] [--projects-dir PATH]
Without --apply it is a dry run and writes nothing.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from app.core.paths import repo_root
from app.core.store_config import configure_default_stores
from app.core.utils import format_errors
from app.models.named_schemas import NamedSchema
from app.services.data_model import DataModel


@dataclass
class ImportPlan:
    """What one pass would store, and the projects it refuses with why."""

    records: list[DataModel] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    parser.add_argument("--projects-dir", type=Path, default=repo_root() / "examples")
    args = parser.parse_args()

    configure_default_stores()
    plan = plan_import(args.projects_dir)
    _report(plan, apply=args.apply)
    if args.apply:
        for record in plan.records:
            record.save()


def plan_import(projects_dir: Path) -> ImportPlan:
    """Every data model this can bring into the store, and the refusals."""
    plan = ImportPlan()
    if not projects_dir.is_dir():
        return plan
    for project_dir in sorted(p for p in projects_dir.iterdir() if p.is_dir()):
        _plan_data_model(project_dir, plan)
    return plan


def _plan_data_model(project_dir: Path, plan: ImportPlan) -> None:
    """`<project>/schemas/*.json` as one DataModel record, or a refusal."""
    schemas_dir = project_dir / "schemas"
    if not schemas_dir.is_dir():
        return
    schemas: list[NamedSchema] = []
    for path in sorted(schemas_dir.glob("*.json")):
        try:
            schemas.append(NamedSchema.model_validate(_read_spec(path)))
        except json.JSONDecodeError as exc:
            plan.refused.append(f"{project_dir.name}/schemas/{path.name}: JSON parse error: {exc}")
            return
        except ValidationError as exc:
            plan.refused.append(
                f"{project_dir.name}/schemas/{path.name}: {'; '.join(format_errors(exc))}"
            )
            return
    if schemas:
        plan.records.append(DataModel(id=project_dir.name, schemas=schemas))


def _read_spec(path: Path) -> dict[str, object]:
    """One schema file's spec — the `_`-prefixed keys the old on-disk reader
    injected are bookkeeping, and NamedSchema forbids extras."""
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValidationError.from_exception_data("NamedSchema", [])
    return {k: v for k, v in document.items() if not k.startswith("_")}


def _report(plan: ImportPlan, *, apply: bool) -> None:
    for line in plan.refused:
        print(f"REFUSED  {line}")
    if not plan.records:
        print("no on-disk data model left to import")
        return
    print(f"{len(plan.records)} data model(s) {'-> storing' if apply else '(dry run)'}:")
    for record in plan.records:
        print(f"  {record.id}: {len(record.schemas)} schema(s)")


if __name__ == "__main__":
    main()

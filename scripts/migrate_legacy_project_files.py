"""One-shot: read the files a project kept before its state moved into the store,
and write them as records. Run it once per store, from the repo root:

    python -m scripts.migrate_legacy_project_files            # plan only
    python -m scripts.migrate_legacy_project_files --apply

Reads only; nothing under the projects root is edited or removed. Event logs are
left where they are — see docs/models-and-storage.md.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from app.core.persistence import get_store
from app.core.store_config import configure_default_document_store, refuse_renamed_env_vars
from app.models.stage import STAGE_SPEC_SCHEMA_VERSION
from app.runtime.manifest import PRODUCTION_RUNS, RunManifest
from app.runtime.stages.human_review_queue import QueueFingerprints
from app.services.loader import WorkingCopy
from app.services.methodology import Methodology
from app.services.project import Project
from app.services.workspace import projects_dir


def main() -> int:
    args = _parse_args()
    refuse_renamed_env_vars()
    configure_default_document_store()
    root = projects_dir()
    if not root.is_dir():
        raise SystemExit(f"no projects root at {root}")

    plan = Plan()
    for project in sorted(p for p in root.iterdir() if p.is_dir()):
        if args.project and project.name != args.project:
            continue
        _plan_project(plan, project, force=args.force)
    _report(plan, applied=args.apply)
    if args.apply:
        for write in plan.writes:
            write()
    # A skip is a record this migration could not honestly build; the caller must
    # see a non-zero exit rather than read "done" over a partial move.
    return 1 if plan.skips else 0


@dataclass
class Plan:
    writes: list = field(default_factory=list)
    counts: Counter[str] = field(default_factory=Counter)
    skips: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add(self, kind: str, write) -> None:
        self.counts[kind] += 1
        self.writes.append(write)

    def skip(self, what: str, why: str) -> None:
        self.skips.append(f"{what}: {why}")

    def note(self, what: str) -> None:
        self.notes.append(what)


def _plan_project(plan: Plan, project: Path, *, force: bool) -> None:
    _note_what_is_left(plan, project)
    _plan_working_copy(plan, project, force=force)
    _plan_methodology(plan, project, force=force)
    _plan_project_record(plan, project, force=force)
    for run_dir in sorted(d for d in (project / "runs").glob("*") if d.is_dir()):
        _plan_run(plan, project.name, run_dir, force=force)


def _note_what_is_left(plan: Plan, project: Path) -> None:
    # Named, because a migration that silently leaves things behind reads as one
    # that moved everything.
    logs = list(project.glob("runs/*/events.jsonl"))
    if logs:
        megabytes = sum(log.stat().st_size for log in logs) / 1e6
        plan.note(f"{project.name}: {len(logs)} event log(s), {megabytes:.0f} MB, left on disk")
    eval_runs = list(project.glob("eval_run/*/manifest.json"))
    if eval_runs:
        plan.note(f"{project.name}: {len(eval_runs)} eval-run manifest(s) left on disk "
                  "— no page reads one")


def _plan_working_copy(plan: Plan, project: Path, *, force: bool) -> None:
    spec_files = sorted((project / "compiled").glob("*.json"))
    if not spec_files or (not force and _stored(WorkingCopy.collection, project.name)):
        return
    specs = []
    for path in spec_files:
        try:
            specs.append(json.loads(path.read_text(encoding="utf-8")))
        except json.JSONDecodeError as exc:
            plan.skip(f"{project.name}/compiled/{path.name}", f"not JSON ({exc})")
    if not specs:
        return
    # Stored unvalidated, in filename order — the same bytes in the same order the
    # directory loader served. A spec today's models reject stays a spec the stage
    # list reports as an issue, which is what it did as a file.
    plan.add("working_copy", lambda name=project.name, specs=specs: _write_specs(name, specs))


def _write_specs(name: str, specs: list[dict]) -> None:
    stored = get_store().read_tolerant(WorkingCopy.collection, name)
    born = stored["created_at"] if stored else _now()
    get_store().write(
        WorkingCopy.collection, name,
        {"id": name, "created_at": born, "updated_at": _now(), "stages": specs},
        schema_version=STAGE_SPEC_SCHEMA_VERSION,
    )


def _plan_methodology(plan: Plan, project: Path, *, force: bool) -> None:
    document = project / "document.md"
    if not document.is_file() or (not force and _stored(Methodology.collection, project.name)):
        return
    text = document.read_text(encoding="utf-8")
    if not text.strip():
        return
    plan.add("methodology",
             lambda name=project.name, text=text: Methodology(id=name, text=text).save())


def _plan_project_record(plan: Plan, project: Path, *, force: bool) -> None:
    legacy = project / "project.json"
    if not legacy.is_file() or (not force and _stored(Project.collection, project.name)):
        return
    stored = json.loads(legacy.read_text(encoding="utf-8"))
    if not isinstance(stored, dict):
        plan.skip(f"{project.name}/project.json", "not an object")
        return
    # project.json's `created_at` is when the PROJECT was authored, which the record
    # calls `authored_at`.
    record = Project(
        id=project.name, name=stored.get("name"), title=stored.get("title"),
        model=stored.get("model"), source=stored.get("source"),
        authored_at=stored.get("created_at"),
    )
    plan.add("project", record.save)


def _plan_run(plan: Plan, project: str, run_dir: Path, *, force: bool) -> None:
    _plan_manifest(plan, project, run_dir, force=force)
    for sidecar in sorted((run_dir / "queue").glob("*.fingerprints.json")):
        _plan_fingerprints(plan, project, run_dir.name, sidecar, force=force)


def _plan_manifest(plan: Plan, project: str, run_dir: Path, *, force: bool) -> None:
    path = run_dir / "manifest.json"
    document_id = RunManifest.compose_id(project, run_dir.name, PRODUCTION_RUNS)
    if not path.is_file() or (not force and _stored(RunManifest.collection, document_id)):
        return
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["id"] = document_id
    raw.setdefault("project", project)
    # The run wrote its own `updated_at`; PersistedModel owns that name now and
    # stamps it on save, so the file's value cannot be carried.
    raw.pop("updated_at", None)
    raw.setdefault("created_at", raw.get("started_at") or _now())
    try:
        manifest = RunManifest.model_validate(raw)
    except ValidationError as exc:
        plan.skip(f"{project}/{run_dir.name}/manifest.json", _first_error(exc))
        return
    plan.add("run", manifest.save)


def _plan_fingerprints(
    plan: Plan, project: str, run_id: str, sidecar: Path, *, force: bool
) -> None:
    stage_id = sidecar.name.removesuffix(".fingerprints.json")
    document_id = QueueFingerprints.compose_id(project, run_id, stage_id)
    if not force and _stored(QueueFingerprints.collection, document_id):
        return
    raw = json.loads(sidecar.read_text(encoding="utf-8"))
    try:
        record = QueueFingerprints(id=document_id, **raw)
    except (ValidationError, TypeError) as exc:
        plan.skip(f"{project}/{run_id}/queue/{sidecar.name}", _first_error(exc))
        return
    plan.add("queue_fingerprints", record.save)


def _stored(collection: str, document_id: str) -> bool:
    return get_store().exists(collection, document_id)


def _now() -> str:
    return datetime.now().isoformat(timespec="microseconds")


def _first_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        first = exc.errors()[0]
        return f"{first['type']} at {'.'.join(str(p) for p in first['loc']) or '<root>'}"
    return str(exc)


def _report(plan: Plan, *, applied: bool) -> None:
    print("WRITING" if applied else "PLAN ONLY — re-run with --apply to write")
    for kind, count in sorted(plan.counts.items()):
        print(f"  {count:5d}  {kind}")
    if not plan.counts:
        print("      0  nothing to migrate")
    for note in plan.notes:
        print(f"  note: {note}")
    for skip in plan.skips:
        print(f"  SKIPPED {skip}", file=sys.stderr)
    if plan.skips:
        print(f"  {len(plan.skips)} item(s) skipped — see above", file=sys.stderr)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write; otherwise print the plan")
    parser.add_argument("--project", help="migrate only this project directory")
    parser.add_argument("--force", action="store_true",
                        help="overwrite records that already exist")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())

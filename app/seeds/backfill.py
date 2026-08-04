"""python -m app.seeds.backfill — mint a Project identity record for each project
directory under the projects root that has a project.json but no record. Dry-run
unless --apply; an existing record is never touched, and no field is inferred from
anything but that project.json."""
from __future__ import annotations

import argparse
from enum import Enum
from pathlib import Path

from pydantic import BaseModel

from app.seeds.bootstrap import configure_projects_dir_from_env, ensure_store_configured
from app.services import workspace
from app.services.project import Project, ProjectFile, read_project_file


class Verdict(str, Enum):
    CREATED = "created"
    ALREADY_RECORDED = "already recorded"
    NOT_A_PROJECT = "not a project"
    MALFORMED = "malformed project.json"


class DirectoryVerdict(BaseModel):
    name: str
    verdict: Verdict
    detail: str | None = None


class BackfillReport(BaseModel):
    applied: bool
    verdicts: list[DirectoryVerdict]

    def count(self, verdict: Verdict) -> int:
        return sum(1 for item in self.verdicts if item.verdict is verdict)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    configure_projects_dir_from_env()
    ensure_store_configured()
    for line in render_report(backfill_project_records(apply=args.apply)):
        print(line)


def backfill_project_records(*, apply: bool) -> BackfillReport:
    """One verdict per directory under the projects root; records are written only when
    `apply` — the default caller sees the plan and the store is left untouched."""
    root = workspace.projects_dir()
    directories = sorted(child for child in root.iterdir() if child.is_dir()) if root.is_dir() else []
    return BackfillReport(
        applied=apply,
        verdicts=[_judge_directory(pdir, apply=apply) for pdir in directories],
    )


def render_report(report: BackfillReport) -> list[str]:
    """The printable report: the root scanned, every directory's verdict, the counts."""
    lines = [f"projects root: {workspace.projects_dir()}"]
    lines += [_render_verdict(item, applied=report.applied) for item in report.verdicts]
    counts = ", ".join(
        f"{report.count(verdict)} {_label_for(verdict, applied=report.applied)}"
        for verdict in Verdict
    )
    lines.append(f"{len(report.verdicts)} directories — {counts}")
    if not report.applied:
        lines.append("DRY RUN: nothing was written. Re-run with --apply to create the records.")
    return lines


def _judge_directory(pdir: Path, *, apply: bool) -> DirectoryVerdict:
    if Project.exists(pdir.name):
        return DirectoryVerdict(name=pdir.name, verdict=Verdict.ALREADY_RECORDED)
    try:
        project_file = read_project_file(pdir)
    except ValueError as exc:
        return DirectoryVerdict(name=pdir.name, verdict=Verdict.MALFORMED, detail=str(exc))
    if project_file is None:
        return DirectoryVerdict(
            name=pdir.name,
            verdict=Verdict.NOT_A_PROJECT,
            detail="no project.json, so nothing on disk states this directory's identity",
        )
    if apply:
        _record_for(pdir.name, project_file).save()
    return DirectoryVerdict(name=pdir.name, verdict=Verdict.CREATED)


def _record_for(name: str, project_file: ProjectFile) -> Project:
    """Carries project.json through verbatim: a key the file omits stays None, never
    inferred from directory mtime, the record's own created_at, or any other proxy."""
    return Project(
        id=name,
        title=project_file.title,
        model=project_file.model,
        source=project_file.source,
        authored_at=project_file.created_at,
    )


def _render_verdict(item: DirectoryVerdict, *, applied: bool) -> str:
    label = _label_for(item.verdict, applied=applied)
    return f"  {item.name}: {label}" + (f" ({item.detail})" if item.detail else "")


def _label_for(verdict: Verdict, *, applied: bool) -> str:
    """A dry run has created nothing yet, so its CREATED verdicts read as a proposal."""
    if verdict is Verdict.CREATED and not applied:
        return "would create"
    return verdict.value


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m app.seeds.backfill",
        description=(
            "Create a Project identity record for each project directory that has a "
            "project.json but no record. Dry-run unless --apply."
        ),
    )
    parser.add_argument(
        "--apply", action="store_true", help="Write the records (default: report only).",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    main()

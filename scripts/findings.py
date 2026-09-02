"""The one check every report's findings land on.

Reads the snapshot pairs `reports` uploaded, asks each report what its branch introduced,
and fails if anything did. A missing snapshot raises: a report that could not run must not
read as a report that found nothing.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from pydantic import BaseModel

from scripts import lexicon, reinvented_functions, unstyled_classes

_REPO_ROOT = Path(__file__).resolve().parents[1]


class Finding(BaseModel):
    report: str
    summary: str
    annotation: str


def find_findings(root: Path) -> list[Finding]:
    return [*find_new_verbs(root), *find_reinventions(root), *find_unstyled_classes(root)]


def find_new_verbs(root: Path) -> list[Finding]:
    head, base = read_pair(root, "lexicon", lexicon.LexiconSnapshot)
    uses = lexicon.find_new_verb_uses(head, base)
    return [
        Finding(report="lexicon", summary=summary, annotation=annotation)
        for summary, annotation in zip(
            lexicon.render_new_verb_lines(uses),
            lexicon.render_new_verb_annotations(uses, head),
            strict=True,
        )
    ]


def find_reinventions(root: Path) -> list[Finding]:
    head, base = read_pair(root, "shapes", reinvented_functions.ShapeSnapshot)
    found = reinvented_functions.find_reinventions(head, base)
    summaries = [
        f"`{one.added.path}:{one.added.line}` `{one.added.name}` "
        f"already exists as `{one.existing[0].name}` in `{one.existing[0].path}`"
        for one in found
    ]
    return _pair_up("reinvented functions", summaries, reinvented_functions.render_annotations(found))


def find_unstyled_classes(root: Path) -> list[Finding]:
    head, base = read_pair(root, "unstyled", unstyled_classes.UnstyledSnapshot)
    offenders = unstyled_classes.find_new_offenders(head, base)
    summaries = [f"`.{one.name}` worn in {', '.join(f'`{p}`' for p in one.paths)}" for one in offenders]
    return _pair_up("unstyled classes", summaries, unstyled_classes.render_annotations(offenders))


def read_pair[SnapshotT: BaseModel](root: Path, name: str, model: type[SnapshotT]) -> tuple[SnapshotT, SnapshotT]:
    head, base = (root / f"head-{name}.json", root / f"base-{name}.json")
    missing = [str(path) for path in (head, base) if not path.is_file()]
    if missing:
        raise FileNotFoundError(
            f"{name} snapshot missing: {missing}. The reports job did not produce it, so this "
            "check cannot tell 'nothing found' from 'never ran'."
        )
    return (
        model.model_validate_json(head.read_text(encoding="utf-8")),
        model.model_validate_json(base.read_text(encoding="utf-8")),
    )


def render_summary(findings: list[Finding]) -> str:
    if not findings:
        return "## No findings\n\nNothing this branch introduced needs a look."
    lines = [f"## {len(findings)} findings", ""]
    for report in dict.fromkeys(one.report for one in findings):
        lines += [f"### {report}", ""]
        lines += [f"- {one.summary}" for one in findings if one.report == report]
        lines.append("")
    lines += [
        "None of this blocks a merge. Fix it, or merge past it — merging IS the acceptance,",
        "and it is the only way to accept: there is no marker, allowlist or label to suppress.",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    findings = find_findings(args.root)
    for finding in findings:
        print(finding.annotation)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        Path(summary).write_text(render_summary(findings), encoding="utf-8")
    else:
        print(render_summary(findings))
    return 1 if findings else 0


def _pair_up(report: str, summaries: list[str], annotations: list[str]) -> list[Finding]:
    return [
        Finding(report=report, summary=summary, annotation=annotation)
        for summary, annotation in zip(summaries, annotations, strict=True)
    ]


if __name__ == "__main__":
    sys.exit(main())

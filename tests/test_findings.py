from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.findings import Finding, find_findings, main, render_summary
from scripts.lexicon import LexiconSnapshot, WordRoles
from scripts.reinvented_functions import ShapeSnapshot, Site
from scripts.unstyled_classes import UnstyledSnapshot

ORIGINAL = Site(path="app/web/values_walk.py", name="build_writer_graph", line=10, nodes=120)
COPY = Site(path="app/web/column_walk.py", name="build_writer_graph", line=14, nodes=120)


def write_snapshots(root: Path, *, shapes: ShapeSnapshot, unstyled: UnstyledSnapshot) -> None:
    words = LexiconSnapshot(words={"walk": WordRoles(noun=3)}, functions=1, accessors=0, types=0)
    for name, head, base in (
        ("lexicon", words, words),
        ("shapes", shapes, ShapeSnapshot(sites={"abc": [ORIGINAL]}, functions=1)),
        ("unstyled", unstyled, UnstyledSnapshot(unstyled={}, declared=1, worn=1)),
    ):
        (root / f"head-{name}.json").write_text(json.dumps(head.model_dump()), encoding="utf-8")
        (root / f"base-{name}.json").write_text(json.dumps(base.model_dump()), encoding="utf-8")


CLEAN = ShapeSnapshot(sites={"abc": [ORIGINAL]}, functions=1)
NOTHING_UNSTYLED = UnstyledSnapshot(unstyled={}, declared=1, worn=1)


def test_a_clean_branch_finds_nothing(tmp_path: Path) -> None:
    write_snapshots(tmp_path, shapes=CLEAN, unstyled=NOTHING_UNSTYLED)
    assert find_findings(tmp_path) == []


def test_findings_from_two_reports_land_on_one_check(tmp_path: Path) -> None:
    write_snapshots(
        tmp_path,
        shapes=ShapeSnapshot(sites={"abc": [ORIGINAL, COPY]}, functions=2),
        unstyled=UnstyledSnapshot(unstyled={"ov-q-body": ["app/templates/a.html"]}, declared=1, worn=2),
    )
    assert [finding.report for finding in find_findings(tmp_path)] == [
        "reinvented functions",
        "unstyled classes",
    ]


def test_a_missing_snapshot_raises_rather_than_reading_as_clean(tmp_path: Path) -> None:
    write_snapshots(tmp_path, shapes=CLEAN, unstyled=NOTHING_UNSTYLED)
    (tmp_path / "base-shapes.json").unlink()
    with pytest.raises(FileNotFoundError, match="never ran"):
        find_findings(tmp_path)


def test_the_check_is_red_only_when_something_was_found(tmp_path: Path) -> None:
    write_snapshots(tmp_path, shapes=CLEAN, unstyled=NOTHING_UNSTYLED)
    assert main(["--root", str(tmp_path)]) == 0
    write_snapshots(
        tmp_path,
        shapes=ShapeSnapshot(sites={"abc": [ORIGINAL, COPY]}, functions=2),
        unstyled=NOTHING_UNSTYLED,
    )
    assert main(["--root", str(tmp_path)]) == 1


def test_the_summary_groups_by_report() -> None:
    summary = render_summary(
        [
            Finding(report="unstyled classes", summary="`.a` worn in `x.html`", annotation="::warning ::a"),
            Finding(report="unstyled classes", summary="`.b` worn in `y.html`", annotation="::warning ::b"),
        ]
    )
    assert summary.count("### unstyled classes") == 1
    assert "2 findings" in summary


def test_the_summary_says_merging_is_the_only_way_to_accept() -> None:
    assert "no marker, allowlist or label" in render_summary(
        [Finding(report="lexicon", summary="walk (verb 0 → 1)", annotation="::error ::walk")]
    )

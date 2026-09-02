from __future__ import annotations

from pathlib import Path

import pytest

from scripts.unstyled_classes import UnstyledSnapshot, build_snapshot, find_new_offenders, render_markdown


def build_tree(root: Path, files: dict[str, str]) -> UnstyledSnapshot:
    for name, text in files.items():
        path = root / "app" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return build_snapshot(root)


def test_a_class_no_stylesheet_declares_is_reported(tmp_path: Path) -> None:
    snapshot = build_tree(tmp_path, {"templates/run.html": '<div class="run-ident">x</div>'})
    assert snapshot.unstyled == {"run-ident": ["app/templates/run.html"]}


def test_a_declared_class_is_not_reported(tmp_path: Path) -> None:
    snapshot = build_tree(
        tmp_path,
        {"templates/run.html": '<div class="run-ident">x</div>', "static/run.css": ".run-ident { color: red }"},
    )
    assert snapshot.unstyled == {}


def test_a_class_declared_in_an_inline_style_block_is_not_reported(tmp_path: Path) -> None:
    page = '<style>.run-ident{color:red}</style><div class="run-ident">x</div>'
    assert build_tree(tmp_path, {"templates/run.html": page}).unstyled == {}


def test_a_class_a_script_reads_is_not_reported(tmp_path: Path) -> None:
    snapshot = build_tree(
        tmp_path,
        {
            "templates/run.html": '<div class="row-pick">x</div>',
            "static/run.js": "document.querySelectorAll('.row-pick')",
        },
    )
    assert snapshot.unstyled == {}


def test_a_class_only_a_class_attribute_mentions_is_still_reported(tmp_path: Path) -> None:
    """Two templates wearing it is not two references — neither one styles it."""
    files = {"templates/a.html": '<i class="lin-disc"></i>', "templates/b.html": '<i class="lin-disc"></i>'}
    assert list(build_tree(tmp_path, files).unstyled) == ["lin-disc"]


def test_a_script_hook_prefix_is_skipped(tmp_path: Path) -> None:
    assert build_tree(tmp_path, {"templates/run.html": '<div class="js-run-log">x</div>'}).unstyled == {}


def test_a_jinja_expression_building_the_name_is_skipped(tmp_path: Path) -> None:
    page = '<div class="{{ tone }}">x</div>'
    assert build_tree(tmp_path, {"templates/run.html": page}).unstyled == {}


def test_an_empty_tree_raises_rather_than_reporting_nothing_wrong(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    with pytest.raises(ValueError, match="misconfigured"):
        build_snapshot(tmp_path)


def snapshot_of(unstyled: dict[str, list[str]]) -> UnstyledSnapshot:
    return UnstyledSnapshot(unstyled=unstyled, declared=100, worn=50)


def test_only_a_class_this_branch_introduces_is_new(tmp_path: Path) -> None:
    head = snapshot_of({"old-debt": ["app/templates/a.html"], "fresh": ["app/templates/b.html"]})
    found = find_new_offenders(head, snapshot_of({"old-debt": ["app/templates/a.html"]}))
    assert [offender.name for offender in found] == ["fresh"]


def test_the_report_names_the_template(tmp_path: Path) -> None:
    body = render_markdown(snapshot_of({"fresh": ["app/templates/b.html"]}), snapshot_of({}))
    assert "`.fresh`" in body
    assert "app/templates/b.html" in body

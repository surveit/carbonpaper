from __future__ import annotations

import ast

from scripts.reinvented_functions import (
    ShapeSnapshot,
    Site,
    describe_shape,
    find_reinventions,
    render_markdown,
)


def shape_of(source: str) -> str | None:
    defined = ast.parse(source).body[0]
    assert isinstance(defined, ast.FunctionDef)
    return describe_shape(defined)


BIG_ENOUGH = """
def keep_recent(rows, cutoff):
    kept = []
    for row in rows:
        if row["at"] >= cutoff and row["state"] != "archived":
            kept.append(row)
    return sorted(kept, key=lambda row: row["at"])
"""

RENAMED_THROUGHOUT = """
def take_fresh(records, floor):
    held = []
    for record in records:
        if record["at"] >= floor and record["state"] != "archived":
            held.append(record)
    return sorted(held, key=lambda record: record["at"])
"""


def test_renaming_every_local_does_not_change_the_shape() -> None:
    assert shape_of(BIG_ENOUGH) == shape_of(RENAMED_THROUGHOUT)


def test_a_different_method_call_is_a_different_shape() -> None:
    deleting = RENAMED_THROUGHOUT.replace("held.append(record)", "held.remove(record)")
    assert shape_of(deleting) != shape_of(RENAMED_THROUGHOUT)


def test_a_body_below_the_floor_is_not_comparable() -> None:
    assert shape_of("def name_of(row):\n    return row['name']\n") is None


def test_a_docstring_does_not_change_the_shape() -> None:
    documented = BIG_ENOUGH.replace("(rows, cutoff):", '(rows, cutoff):\n    """Rows since the cutoff."""')
    assert shape_of(documented) == shape_of(BIG_ENOUGH)


def snapshot(sites: dict[str, list[Site]], functions: int = 10) -> ShapeSnapshot:
    return ShapeSnapshot(sites=sites, functions=functions)


ORIGINAL = Site(path="app/web/values_walk.py", name="build_writer_graph", line=10, nodes=120)
COPY = Site(path="app/web/column_walk.py", name="build_writer_graph", line=14, nodes=120)


def test_a_copy_beside_a_surviving_original_is_reported() -> None:
    head = snapshot({"abc": [ORIGINAL, COPY]})
    found = find_reinventions(head, snapshot({"abc": [ORIGINAL]}))
    assert [(site.added.path, site.existing[0].path) for site in found] == [(COPY.path, ORIGINAL.path)]


def test_a_rename_is_not_a_reinvention() -> None:
    renamed = Site(path=ORIGINAL.path, name="draw_writer_graph", line=10, nodes=120)
    assert find_reinventions(snapshot({"abc": [renamed]}), snapshot({"abc": [ORIGINAL]})) == []


def test_a_new_shape_nothing_shares_is_not_reported() -> None:
    assert find_reinventions(snapshot({"abc": [COPY]}), snapshot({})) == []


def test_the_report_names_both_sites() -> None:
    body = render_markdown(snapshot({"abc": [ORIGINAL, COPY]}), snapshot({"abc": [ORIGINAL]}, functions=9))
    assert "app/web/column_walk.py:14" in body
    assert "app/web/values_walk.py" in body
    assert "(+1)" in body

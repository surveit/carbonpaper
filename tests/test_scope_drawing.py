"""Where the scope map puts things. Every rule here was a bug caught by eye first."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.web.scope_drawing import BAND_GAP, HEAD, ScopeDrawing, draw_the_scope
from app.web.scope_view import load_scope_map
from app.models.claims import StageOutputCellCitation
from test_scope_page import (  # noqa: F401  (the fixtures come with them)
    PROJECT,
    TIERED_PROJECT,
    run_id,
    scope_url,
    tiered_run_id,
)


def read_map(project: str, run: str, stage: str, column: str):
    cited = StageOutputCellCitation(stage_id=stage, row_ordinal=0, column=column,
                                    run_id=run, value=None)
    scope, _cuts = load_scope_map(project, run, cited)
    return scope


def both_drawings(scope) -> list[ScopeDrawing]:
    return [draw_the_scope(scope, every_stage=False),
            draw_the_scope(scope, every_stage=True)]


@pytest.fixture
def totals(run_id):  # noqa: F811
    return read_map(PROJECT, run_id, "grant_totals", "total_amount")


def test_a_bar_holds_only_rows_that_came_through_its_column(totals):
    came = {ordinal: set(totals.came_through[totals.came_through_index[at]])
            for at, ordinal in enumerate(totals.covers.ordinals)}
    for drawing in both_drawings(totals):
        for column in drawing.columns:
            for bar in column.bars:
                assert bar.on, f"{bar.key} holds no rows"
                strays = [row for row in bar.on if column.stage.id not in came[row]]
                assert not strays, f"{bar.key} holds {strays}, never rows of that frame"


def test_every_row_is_on_one_bar_of_each_column_it_came_through(totals):
    for drawing in both_drawings(totals):
        for column in drawing.columns:
            seen = [row for bar in column.bars for row in bar.on]
            assert len(seen) == len(set(seen)), f"{column.stage.id} drew a row twice"


# A ratchet: entries may be removed, never added.
KNOWN_COLLISIONS = frozenset({
    ("load_west", "load_west load_west|loaded",
     "load_east load_east|loaded>tag_portfolio tag_portfolio|missed:load_agencies"),
})


def test_no_bar_newly_sits_inside_a_ribbon_running_past_its_column(totals):
    found = set()
    for drawing in both_drawings(totals):
        for column in drawing.columns:
            for ribbon in _running_past(drawing, column):
                found.update(
                    (column.stage.id, bar.key,
                     f"{ribbon.from_key}>{ribbon.into_key}")
                    for bar in column.bars if _overlaps(bar, ribbon))
    assert found - KNOWN_COLLISIONS == set(), "a ribbon runs through a new bar"
    assert KNOWN_COLLISIONS - found == set(), "a known collision is gone; drop it"


def test_the_band_starts_clear_of_the_head(totals):
    for drawing in both_drawings(totals):
        assert drawing.top >= HEAD + BAND_GAP
        for column in drawing.columns:
            for bar in column.bars:
                assert bar.y >= drawing.top


def test_a_source_stacks_below_the_rows_already_running_past_it(totals):
    every = draw_the_scope(totals, every_stage=True)
    entered = {}
    for at, column in enumerate(every.columns):
        for bar in column.bars:
            for row in bar.on:
                entered.setdefault(row, at)
    for at, column in enumerate(every.columns):
        starting = [b for b in column.bars if all(entered[r] == at for r in b.on)]
        running = sum(1 for row, first in entered.items() if first < at
                      and row not in {r for b in column.bars for r in b.on})
        if starting and running:
            assert min(b.y for b in starting) > every.top, (
                f"{column.stage.id} starts a source at the top of the band")


def test_no_stage_behind_a_lookup_is_drawn(tiered_run_id):  # noqa: F811
    scope = read_map(TIERED_PROJECT, tiered_run_id, "grant_totals", "total_amount")
    for drawing in both_drawings(scope):
        drawn = {column.stage.id for column in drawing.columns}
        assert not drawn & set(scope.lookup_tables)


def test_the_drawing_is_what_the_route_serves(run_id):  # noqa: F811
    payload = TestClient(app).get(
        scope_url(PROJECT, run_id, "grant_totals", "total_amount", 0,
                  suffix=".json")).json()
    assert payload["drawn"]["columns"], "the route served no drawing"
    assert (len(payload["drawn_every_stage"]["columns"])
            >= len(payload["drawn"]["columns"]))


def _running_past(drawing: ScopeDrawing, column) -> list:
    edge = column.x + drawing.bar_width
    return [r for r in drawing.ribbons if r.x0 < column.x and r.x1 > edge]


def _overlaps(bar, ribbon) -> bool:
    top, bottom = _edges_at(ribbon, bar.x)
    return bar.y < bottom and top < bar.y + bar.height


def _edges_at(ribbon, at_x: float) -> tuple[float, float]:
    """Both edges of the curve where it passes x, walked rather than solved."""
    middle = (ribbon.x0 + ribbon.x1) / 2
    best = (float("inf"), 0.0, 0.0)
    for step in range(501):
        t = step / 500
        x = _cubic(ribbon.x0, middle, middle, ribbon.x1, t)
        if abs(x - at_x) < best[0]:
            best = (abs(x - at_x),
                    _cubic(ribbon.y0, ribbon.y0, ribbon.y1, ribbon.y1, t),
                    _cubic(ribbon.y0 + ribbon.h0, ribbon.y0 + ribbon.h0,
                           ribbon.y1 + ribbon.h1, ribbon.y1 + ribbon.h1, t))
    return best[1], best[2]


def _cubic(a: float, b: float, c: float, d: float, t: float) -> float:
    return ((1 - t) ** 3 * a + 3 * (1 - t) ** 2 * t * b
            + 3 * (1 - t) * t ** 2 * c + t ** 3 * d)


def test_the_stages_below_the_rows_own_stage_are_still_drawn(run_id):  # noqa: F811
    # This figure walks down two merges, so stages sit between its rows and it.
    scope = read_map(PROJECT, run_id, "total_of_means", "summed_means")
    assert scope.covers.at_stage != scope.citation.stage_id
    for drawing in both_drawings(scope):
        drawn = {column.stage.id: len(column.bars) for column in drawing.columns}
        assert drawn.get(scope.citation.stage_id), "the drawing stops above the figure"
        assert not [at for at, bars in drawn.items() if not bars]


def test_a_cell_of_the_frame_it_was_read_into_is_merged_from_nothing(run_id):  # noqa: F811
    scope = read_map(PROJECT, run_id, "load_east", "amount")
    bar = next(b for d in both_drawings(scope) for c in d.columns
               for b in c.bars if b.is_figure)
    assert "merged from" not in bar.label
    assert bar.label.startswith("amount = ")


def test_one_column_is_drawn_no_taller_than_its_labels(run_id):  # noqa: F811
    scope = read_map(PROJECT, run_id, "load_east", "amount")
    for drawing in both_drawings(scope):
        assert len(drawing.columns) == 1
        assert not drawing.ribbons
        assert drawing.height < 200, "a lone row was drawn as a slab"

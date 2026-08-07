from __future__ import annotations

import pandas as pd
import pytest

from evals.harness.case import Case
from evals.harness.compare import compare_case, compare_case_csv

_ROWS = [
    {"state": "Alpha", "pop": "739482", "rate": "6.027192"},
    {"state": "Beta", "pop": "584153", "rate": "3.100000"},
    {"state": "Gamma", "pop": "100000", "rate": "0.037419"},
]


def _case(rows: list[dict[str, str | None]] | None = None) -> Case:
    return Case.model_validate(
        {
            "case_id": "t",
            "source": {"repo": "https://example.invalid/r", "commit": "0" * 40, "path": "n.ipynb"},
            "inputs": [{"path": "a.csv", "sha256": "f" * 64}],
            "brief": "tell a story; rank by rate descending, ties by state ascending",
            "golden": {"columns": ["state", "pop", "rate"], "rows": rows or _ROWS},
            "tolerance": 1e-6,
        }
    )


def _frame(rows: list[dict[str, object]]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def test_a_rendered_golden_agrees_with_a_typed_output():
    """Golden cells are TEXT off the notebook; a build's are int/float."""
    comparison = compare_case(
        _case(),
        _frame(
            [
                {"state": "Alpha", "pop": 739482.0, "rate": 6.0271920073781375},
                {"state": "Beta", "pop": 584153, "rate": 3.1},
                {"state": "Gamma", "pop": 100000, "rate": 0.037419484898878},
            ]
        ),
    )
    assert comparison.agrees()
    assert comparison.aligned_rows == 3


def test_the_same_rows_in_the_wrong_order_do_not_agree():
    """The sort is part of the answer, so a reordered table is a difference."""
    comparison = compare_case(
        _case(),
        _frame(
            [
                {"state": "Beta", "pop": 584153, "rate": 3.1},
                {"state": "Alpha", "pop": 739482, "rate": 6.027192},
                {"state": "Gamma", "pop": 100000, "rate": 0.037419},
            ]
        ),
    )
    assert not comparison.agrees()


def test_a_row_only_in_the_golden_reads_as_one_missing_row():
    """Not a cascade over every row after it."""
    comparison = compare_case(
        _case(),
        _frame(
            [
                {"state": "Alpha", "pop": 739482, "rate": 6.027192},
                {"state": "Gamma", "pop": 100000, "rate": 0.037419},
            ]
        ),
    )
    assert [(d.kind, d.position, d.row["state"]) for d in comparison.row_differences] == [
        ("missing", 1, "Beta")
    ]
    assert comparison.cell_differences == []
    assert comparison.aligned_rows == 2


def test_a_row_only_in_the_build_reads_as_one_extra_row():
    comparison = compare_case(
        _case(),
        _frame(
            [
                {"state": "Alpha", "pop": 739482, "rate": 6.027192},
                {"state": "Beta", "pop": 584153, "rate": 3.1},
                {"state": "Gamma", "pop": 100000, "rate": 0.037419},
                {"state": "Delta", "pop": 1, "rate": 0.0},
            ]
        ),
    )
    assert [(d.kind, d.position, d.row["state"]) for d in comparison.row_differences] == [
        ("extra", 3, "Delta")
    ]


def test_a_figure_outside_tolerance_is_reported_against_its_position():
    comparison = compare_case(
        _case(),
        _frame(
            [
                {"state": "Alpha", "pop": 739482, "rate": 6.03},
                {"state": "Beta", "pop": 584153, "rate": 3.1},
                {"state": "Gamma", "pop": 100000, "rate": 0.037419},
            ]
        ),
    )
    assert [(d.position, d.column) for d in comparison.cell_differences] == [(0, "rate")]


def test_a_coarsely_aligned_pair_is_still_checked_at_full_tolerance():
    """Alignment rounds to 4 significant figures; that must not pass a real difference."""
    comparison = compare_case(
        _case([{"state": "Alpha", "pop": "1000", "rate": "1.000000"}]),
        _frame([{"state": "Alpha", "pop": 1000, "rate": 1.0004}]),
    )
    assert comparison.aligned_rows == 1
    assert [(d.position, d.column) for d in comparison.cell_differences] == [(0, "rate")]


def test_tolerance_scales_with_the_golden_value():
    """A golden printed in scientific notation carries significant figures, not decimals."""
    case = _case([{"state": "Alpha", "pop": "1", "rate": "2.073536e+08"}])
    assert compare_case(case, _frame([{"state": "Alpha", "pop": 1, "rate": 207353612.4}])).agrees()
    assert not compare_case(
        case, _frame([{"state": "Alpha", "pop": 1, "rate": 207400000.0}])
    ).agrees()


def test_an_absent_cell_agrees_only_with_an_absent_cell():
    case = _case([{"state": "Alpha", "pop": "1", "rate": None}])
    assert compare_case(case, _frame([{"state": "Alpha", "pop": 1, "rate": None}])).agrees()
    assert not compare_case(case, _frame([{"state": "Alpha", "pop": 1, "rate": 0.0}])).agrees()


def test_a_zero_padded_value_survives_reading_a_build_csv(tmp_path):
    """pandas' default inference turns `06055` into `6055`."""
    case = _case([{"state": "06055", "pop": "1", "rate": "2.0"}])
    csv = tmp_path / "build.csv"
    csv.write_text("state,pop,rate\n06055,1,2.0\n", encoding="utf-8")
    assert compare_case_csv(case, csv).agrees()


def test_a_missing_column_raises_naming_what_exists():
    with pytest.raises(ValueError, match="no column"):
        compare_case(_case(), _frame([{"state": "Alpha", "pop": 1}]))

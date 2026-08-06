from __future__ import annotations

import pandas as pd
import pytest

from evals.harness.case import Case
from evals.harness.compare import compare_case


def _case(**overrides: object) -> Case:
    spec: dict[str, object] = {
        "case_id": "t",
        "source": {
            "repo": "https://example.invalid/r",
            "commit": "0" * 40,
            "notebook_path": "n.ipynb",
            "cell_index": 0,
        },
        "inputs": [{"path": "a.csv", "sha256": "f" * 64}],
        "brief": "tell a story",
        "golden": {
            "key_column": "jurisdiction",
            "columns": ["jurisdiction", "pop", "rate"],
            "rows": [
                {"jurisdiction": "Alpha", "pop": "739482", "rate": "6.027192"},
                {"jurisdiction": "Beta", "pop": "584153", "rate": "0.000000"},
            ],
        },
        "comparison": {
            "output_key_column": "jurisdiction",
            "compared_columns": {"jurisdiction": "jurisdiction", "pop": "pop", "rate": "rate"},
            "tolerance": 1e-6,
        },
    }
    spec.update(overrides)
    return Case.model_validate(spec)


def test_a_rendered_golden_agrees_with_a_typed_output():
    """The golden's cells are TEXT off the notebook; the build's are int/float."""
    actual = pd.DataFrame(
        [
            {"jurisdiction": "Alpha", "pop": 739482.0, "rate": 6.0271920073781375},
            {"jurisdiction": "Beta", "pop": 584153, "rate": 0.0},
        ]
    )
    comparison = compare_case(_case(), actual)
    assert comparison.figure_disagreements == []
    assert comparison.agrees()


def test_a_key_only_in_the_golden_is_reported_as_missing():
    actual = pd.DataFrame([{"jurisdiction": "Alpha", "pop": 739482, "rate": 6.027192}])
    comparison = compare_case(_case(), actual)
    assert comparison.missing_from_output == ["Beta"]
    assert comparison.figure_disagreements == []
    assert not comparison.agrees()


def test_a_figure_outside_tolerance_is_reported():
    actual = pd.DataFrame(
        [
            {"jurisdiction": "Alpha", "pop": 739482, "rate": 6.03},
            {"jurisdiction": "Beta", "pop": 584153, "rate": 0.0},
        ]
    )
    comparison = compare_case(_case(), actual)
    assert [(d.key, d.column) for d in comparison.figure_disagreements] == [("Alpha", "rate")]


def test_a_duplicated_output_key_raises_rather_than_picking_one():
    actual = pd.DataFrame(
        [
            {"jurisdiction": "Alpha", "pop": 739482, "rate": 6.027192},
            {"jurisdiction": "Alpha", "pop": 1, "rate": 2.0},
        ]
    )
    with pytest.raises(ValueError, match="more than once"):
        compare_case(_case(), actual)


def test_a_missing_compared_column_raises_naming_what_exists():
    actual = pd.DataFrame([{"jurisdiction": "Alpha", "pop": 739482}])
    with pytest.raises(ValueError, match="no column"):
        compare_case(_case(), actual)

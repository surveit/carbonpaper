"""Every shipped example methodology must satisfy the stage contract.

This is the regression net for the strict loader: if a compiled stage file
drifts from the models (or a model change breaks the shipped DAGs), this fails
with the exact per-file issues.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.models import Stage, validate_methodology_stages
from app.models.schema import format_errors
from pydantic import ValidationError

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
COMPILED_DIRS = sorted(p for p in EXAMPLES.glob("*/compiled") if p.is_dir())


@pytest.mark.parametrize("compiled_dir", COMPILED_DIRS, ids=lambda p: p.parent.name)
def test_example_dag_satisfies_contract(compiled_dir: Path):
    stages, issues = [], []
    for f in sorted(compiled_dir.glob("*.yaml")):
        try:
            stages.append(Stage.model_validate(yaml.safe_load(f.read_text(encoding="utf-8"))))
        except ValidationError as err:
            issues += [f"{f.name}: {i}" for i in format_errors(err)]
    issues += validate_methodology_stages(stages)
    assert not issues, "\n".join(issues)

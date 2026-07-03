"""Every shipped example methodology must satisfy the stage contract.

This is the regression net for the strict loader: if a compiled stage file
drifts from the models (or a model change breaks the shipped DAGs), this fails
with the exact per-file issues.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.models import validate_methodology_stages
from app.models.loader import load_compiled_dir

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
COMPILED_DIRS = sorted(p for p in EXAMPLES.glob("*/compiled") if p.is_dir())


@pytest.mark.parametrize("compiled_dir", COMPILED_DIRS, ids=lambda p: p.parent.name)
def test_example_dag_satisfies_contract(compiled_dir: Path):
    entries = load_compiled_dir(compiled_dir)
    issues = [f"{e.filename}: {i}" for e in entries for i in e.issues]
    stages = [e.stage for e in entries if e.stage is not None]
    issues += validate_methodology_stages(stages)
    assert stages, f"no compiled stages found in {compiled_dir}"
    assert not issues, "\n".join(issues)

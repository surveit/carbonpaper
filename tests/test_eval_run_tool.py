"""The agent tool that runs an eval: the run the eval page's own button makes, handed
back with the URL of the page that shows which rows disagreed."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from app.models import (
    ExpectedOutput,
    ScoringMetric,
    TableRef,
    parse_stage,
)
from app.models.records.eval_config import EvalConfig
from app.models.schema import TableSchema
from app.models.stages.input_data import FileFormat
from app.services.project import write_eval_config
from app.models.records.workflow_version import WorkflowVersion
from app.tools.eval_runs import run_eval

_BASE_URL = "http://127.0.0.1:8788"

# The eval injects this stage's output, so the file it names is never opened.
_LOAD = {
    "id": "load", "type": "input_data", "description": "Load rows",
    "connector": {"kind": "file", "params": {"path": "/rows.csv", "format": "csv"}},
    "signature": {
        "form": "replaces",
        "produces": [
            {"name": "doc_id", "type": "str", "nullable": True},
            {"name": "score", "type": "int", "nullable": True},
        ],
    },
}
# label = "pos" iff score >= 0 — a deterministic classifier the eval can predict.
_CLASSIFY = {
    "id": "classify", "type": "python_row_function", "description": "Label by sign",
    "inputs": [{"id": "load"}],
    "function": {"kind": "inline", "code":
                 "def transform(row):\n"
                 "    return {'doc_id': row['doc_id'], 'score': row['score'],\n"
                 "            'label': 'pos' if row['score'] >= 0 else 'neg'}"},
    "signature": {
        "form": "extends",
        "reads": [{"input": "load", "columns": [
            {"name": "doc_id", "type": "str", "nullable": True},
            {"name": "score", "type": "int", "nullable": True}]}],
        "adds": [{"name": "label", "type": "str", "nullable": True}],
    },
}


@pytest.fixture
def demo(projects_root: Path, tmp_path: Path) -> str:
    (projects_root / "demo").mkdir(parents=True, exist_ok=True)
    WorkflowVersion(
        id="demo/v1", version_id="v1", created_at="2026-07-10T00:00:00",
        message="seed",
        stages=[parse_stage(_LOAD), parse_stage(_CLASSIFY)],
    ).save()
    cases = tmp_path / "cases.csv"
    # classify says pos for score >= 0, so the expected label disagrees on doc c.
    pd.DataFrame({"doc_id": ["a", "b", "c", "d"], "score": [1, -1, 2, -3],
                  "label": ["pos", "neg", "neg", "neg"]}).to_csv(cases, index=False)
    write_eval_config("demo", EvalConfig(
        eval_id="label_check", project="demo", name="Label check",
        override_stage="load", target_stage="classify",
        table=TableRef(path=str(cases), format=FileFormat.csv,
                       table_schema=TableSchema.model_validate({"columns": [
                           {"name": "doc_id", "type": "str", "nullable": True},
                           {"name": "score", "type": "int", "nullable": True},
                           {"name": "label", "type": "str", "nullable": True}]})),
        expected_outputs=[
            ExpectedOutput(output_column="label", metric=ScoringMetric.exact)]))
    return "demo"


def test_the_tool_scores_the_eval_and_links_the_page_that_shows_the_rows(demo: str) -> None:
    result = run_eval(demo, "label_check", base_url=_BASE_URL)

    assert result.run.status == "scored"
    assert result.run.workflow_version == "v1"
    assert result.run.metrics["rows_scored"] == 4
    assert result.run.metrics["rows_passed"] == 3
    assert result.run_url == (
        f"{_BASE_URL}/project/demo/evals/label_check/runs/{result.run.run_id}"
    )


def test_an_eval_the_project_does_not_have_is_refused(demo: str) -> None:
    with pytest.raises(FileNotFoundError):
        run_eval(demo, "no_such_eval")


"""The agent tool that runs an eval: the run the eval page's own button makes, handed
back with the URL of the page that shows which rows disagreed."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.models import EvalConfig, ExpectedOutput, ScoringMetric, TableRef, parse_stage
from app.models.schema import TableSchema
from app.models.stages.input_data import FileFormat
from app.evals.store import save_dataset_upload
from app.services.project import write_eval_config
from app.services.versioning import WorkflowVersion
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
def demo(projects_root: Path) -> str:
    project_dir = projects_root / "demo"
    project_dir.mkdir(parents=True, exist_ok=True)
    WorkflowVersion(
        id="demo/v1", version_id="v1", created_at="2026-07-10T00:00:00",
        message="seed", reviewer="test",
        stages=[parse_stage(_LOAD), parse_stage(_CLASSIFY)],
    ).save()
    # classify says pos for score >= 0, so the expected label disagrees on doc c.
    cases = save_dataset_upload(project_dir, "cases.csv", (
        b"doc_id,score,label\na,1,pos\nb,-1,neg\nc,2,neg\nd,-3,neg\n"))
    write_eval_config("demo", EvalConfig(
        id="label_check", project="demo", name="Label check",
        override_stage="load", target_stage="classify",
        table=TableRef(path=cases, format=FileFormat.csv,
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
        f"{_BASE_URL}/project/demo/evals/label_check/runs/{result.run.id}"
    )


def test_an_eval_the_project_does_not_have_is_refused(demo: str) -> None:
    with pytest.raises(FileNotFoundError):
        run_eval(demo, "no_such_eval")


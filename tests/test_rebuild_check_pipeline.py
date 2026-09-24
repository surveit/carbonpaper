"""The outer pipeline's code stages on hand-made rows, and the committed eval against it."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.evals.compatibility import validate_eval_compatibility
from app.evals.dataset import read_table_ref
from app.models import parse_workflow
from app.models.records.eval_config import EvalConfig
from evals.rebuild_check import build_run_evals
from evals.rebuild_check.eval_pieces import COMPARE_CODE, VERDICT_CODE, render_resolve_sources_code

_CONFIG = Path(build_run_evals.__file__).with_name("eval_config.json")
_SCHEMA = json.dumps({"type": "object", "properties": {
    "count": {"type": "integer"}, "share": {"type": "number"}, "date": {"type": "string"}}})


def _load(code: str) -> Any:
    namespace: dict[str, Any] = {}
    exec(code, namespace)
    return namespace["transform"]


def _compare(expected: dict, computed: dict) -> dict:
    out = _load(COMPARE_CODE)({"expected_json": json.dumps(expected),
                               "results_json": json.dumps(computed), "target_schema": _SCHEMA})
    return {**out, "comparison": json.loads(out["comparison_json"])}


def test_compare_grades_each_figure_type_first_then_value():
    out = _compare({"count": 17, "share": "more than 30 percent", "date": "May 2018"},
                   {"count": 17, "share": 31.8, "extra": 2})
    verdicts = {field: c["verdict"] for field, c in out["comparison"].items()}
    assert verdicts == {"count": "agrees", "share": "differs", "date": "not_computed",
                        "extra": "not_expected"}
    assert out["fields_compared"] == 4
    assert len(out["findings_text"].splitlines()) == 4


def test_a_value_of_the_wrong_type_is_wrong_type_not_a_difference():
    out = _compare({"count": 17}, {"count": "17"})
    assert out["comparison"]["count"]["verdict"] == "wrong_type"


def _row(rulings: list[dict], *, citation: bool = True, process: bool = True) -> dict:
    return {"citation_holds": citation, "process_ok": process, "diagnosis": json.dumps(rulings)}


@pytest.mark.parametrize("rulings, passed", [
    ([], True),
    ([{"field": "a", "verdict": "consistent"}, {"field": "b", "verdict": "their_defect"},
      {"field": "c", "verdict": "genuine_ambiguity"}], True),
    ([{"field": "a", "verdict": "consistent"}, {"field": "b", "verdict": "our_defect"}], False),
])
def test_a_case_passes_unless_a_figure_is_our_defect(rulings, passed):
    assert _load(VERDICT_CODE)(_row(rulings)) == {"passed": passed}


def test_a_case_fails_when_its_citation_or_process_does_not_hold():
    assert _load(VERDICT_CODE)(_row([], citation=False)) == {"passed": False}
    assert _load(VERDICT_CODE)(_row([], process=False)) == {"passed": False}


@pytest.mark.parametrize("diagnosis", [
    json.dumps([{"field": "a", "verdict": "probably_fine"}]),
    json.dumps({"field": "a", "verdict": "our_defect"}),
])
def test_a_ruling_the_verdict_cannot_read_stops_the_case(diagnosis):
    with pytest.raises(ValueError):
        _load(VERDICT_CODE)({"citation_holds": True, "process_ok": True, "diagnosis": diagnosis})


def test_sources_resolve_against_the_checkout_and_a_missing_one_stops_the_case(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "one.csv").write_text("x\n1\n", encoding="utf-8")
    transform = _load(render_resolve_sources_code(tmp_path))
    assert transform({"input_files": "a/one.csv"}) == {
        "input_paths": (tmp_path / "a" / "one.csv").as_posix()}
    with pytest.raises(FileNotFoundError, match="a/gone.csv"):
        transform({"input_files": "a/gone.csv"})


def _config() -> EvalConfig:
    return EvalConfig.model_validate(
        {**json.loads(_CONFIG.read_text(encoding="utf-8")), "project": "p"})


def test_the_committed_eval_scores_the_outer_pipeline_by_position():
    report = validate_eval_compatibility(_config(), parse_workflow(build_run_evals.stages()))
    assert report.ok, report.problems
    assert report.settings is not None and report.settings.can_score_declaratively


def test_the_committed_cases_read_as_the_eval_dataset():
    config = _config()
    assert config.table is not None
    frame = read_table_ref(config.table)
    assert set(frame.columns) == {c.name for c in config.table.table_schema.columns}

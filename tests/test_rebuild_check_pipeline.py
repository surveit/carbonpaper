"""The outer pipeline's code stages on hand-made rows, and the committed eval against it."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from app.core.paths import repo_root
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


def _agreement(*fields: str) -> dict:
    return {field: {"verdict": "agrees"} for field in fields}


def _disagreement(*fields: str) -> dict:
    return {field: {"verdict": "differs"} for field in fields}


def _row(rulings: list[dict], comparison: dict, *,
        citation: bool = True, process: bool = True, diagnosis: str | None = None) -> dict:
    return {"citation_holds": citation, "process_ok": process,
            "diagnosis": json.dumps(rulings) if diagnosis is None else diagnosis,
            "comparison_json": json.dumps(comparison)}


@pytest.mark.parametrize("rulings, comparison, passed", [
    ([], _agreement("a"), True),
    ([{"field": "a", "verdict": "consistent"}, {"field": "b", "verdict": "their_defect"},
      {"field": "c", "verdict": "genuine_ambiguity"}], _disagreement("a", "b", "c"), True),
    ([{"field": "a", "verdict": "consistent"}, {"field": "b", "verdict": "our_defect"}],
     _disagreement("a", "b"), False),
])
def test_a_case_passes_unless_a_figure_is_our_defect(rulings, comparison, passed):
    assert _load(VERDICT_CODE)(_row(rulings, comparison))["passed"] == passed


def test_a_case_fails_when_its_citation_or_process_does_not_hold():
    citation_failure = _load(VERDICT_CODE)(_row([], _agreement("a"), citation=False))
    assert citation_failure == {"passed": False, "verdict_reason": "the citation did not hold"}
    process_failure = _load(VERDICT_CODE)(_row([], _agreement("a"), process=False))
    assert process_failure == {
        "passed": False, "verdict_reason": "the judge did not trust the comparison"}


def test_a_figure_the_judge_left_unruled_fails_the_case():
    out = _load(VERDICT_CODE)(_row([], _disagreement("a", "b")))
    assert out["passed"] is False
    assert "a, b" in out["verdict_reason"]


def test_a_ruling_on_a_field_the_comparison_does_not_carry_fails_the_case():
    out = _load(VERDICT_CODE)(
        _row([{"field": "not_a_field", "verdict": "their_defect"}], _disagreement("a")))
    assert out["passed"] is False
    assert "not_a_field" in out["verdict_reason"]


def test_a_ruling_with_an_unknown_verdict_value_fails_the_case():
    out = _load(VERDICT_CODE)(
        _row([{"field": "a", "verdict": "probably_fine"}], _disagreement("a")))
    assert out["passed"] is False
    assert "probably_fine" in out["verdict_reason"]


@pytest.mark.parametrize("diagnosis", [
    "not json",
    json.dumps({"field": "a", "verdict": "our_defect"}),
])
def test_an_unreadable_diagnosis_fails_the_case(diagnosis):
    out = _load(VERDICT_CODE)(_row([], _disagreement("a"), diagnosis=diagnosis))
    assert out == {"passed": False,
                   "verdict_reason": "the judge's diagnosis could not be read: " + diagnosis}


def test_sources_resolve_against_the_checkout_and_a_missing_one_stops_the_case(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "one.csv").write_text("x\n1\n", encoding="utf-8")
    transform = _load(render_resolve_sources_code(tmp_path))
    assert transform({"input_files": "a/one.csv"}) == {
        "input_paths": (tmp_path / "a" / "one.csv").as_posix()}
    with pytest.raises(FileNotFoundError, match="a/gone.csv"):
        transform({"input_files": "a/gone.csv"})


def test_a_case_with_no_source_files_is_refused_by_name(tmp_path):
    transform = _load(render_resolve_sources_code(tmp_path))
    with pytest.raises(ValueError, match="the case lists no source files"):
        transform({"input_files": ""})
    with pytest.raises(ValueError, match="the case lists no source files"):
        transform({"input_files": None})


def test_an_absolute_source_path_is_refused(tmp_path):
    transform = _load(render_resolve_sources_code(tmp_path))
    absolute = str(tmp_path / "outside.csv")
    with pytest.raises(ValueError, match=re.escape(absolute)):
        transform({"input_files": absolute})


def test_a_source_path_that_escapes_the_checkout_root_is_refused(tmp_path):
    (tmp_path / "a").mkdir()
    transform = _load(render_resolve_sources_code(tmp_path / "a"))
    with pytest.raises(ValueError, match="outside the checkout root"):
        transform({"input_files": "../outside.csv"})


def test_every_committed_case_input_files_resolves_inside_the_checkout():
    root = repo_root()
    transform = _load(render_resolve_sources_code(root))
    items_path = root / "evals" / "rebuild_check" / "items" / "eval_items.json"
    lines = [line for line in items_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert lines
    for line in lines:
        item = json.loads(line)
        result = transform({"input_files": item["input_files"]})
        for resolved in result["input_paths"].split(";"):
            assert Path(resolved).is_relative_to(root)
            assert Path(resolved).is_file()


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


def test_an_eval_run_never_reaches_the_claims_step():
    report = validate_eval_compatibility(_config(), parse_workflow(build_run_evals.stages()))
    assert report.settings is not None and "claims" not in report.settings.frontier
    assert build_run_evals.stages()[-1]["id"] == "claims"

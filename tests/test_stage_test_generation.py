"""The generator bridge: task assembly is code-blind, document-blind, and grounded
in the step's own description."""
import pytest

from app.compiler.stage_tests import build_stage_test_generator, render_generation_task
from app.models import parse_stage, Stage

_CODE = "def transform(row):\n    return {**row, 'doubled': row['amount'] * 2}\n"
_SUMMARY = "Doubles the reported `amount` into `doubled`."
_DOC = "----doc text----"


def _python_stage(*, summary=_SUMMARY, corner_cases=None) -> Stage:
    function = {"kind": "inline", "code": _CODE, "summary": summary}
    if corner_cases is not None:
        function["corner_cases"] = corner_cases
    return parse_stage({
        "id": "double", "name": "Double", "type": "python_row_function",
        "inputs": [{"id": "load", "schema": {"columns": [
            {"name": "amount", "type": "float", "nullable": False},
        ]}}],
        "function": function,
        "signature": {
            "form": "extends",
            "reads": [
                {
                    "input": "load",
                    "columns": [{"name": "amount", "type": "float", "nullable": False}],
                },
            ],
            "adds": [{"name": "doubled", "type": "float", "nullable": False}],
        },
    })


def test_task_contains_the_description_schemas_and_stage_meta():
    task = render_generation_task(_DOC, _python_stage())
    assert _SUMMARY in task
    assert "Double" in task            # stage name rendered
    assert "double" in task            # stage id rendered
    assert "doubled" in task           # output schema rendered
    assert "load" in task              # input id rendered


def test_task_never_contains_the_methodology_document():
    """The examples exist to check the code against the DESCRIPTION. An agent that
    had read the methodology could write a case the description never implies, and
    the suite would then certify the methodology instead."""
    task = render_generation_task(_DOC, _python_stage())
    assert _DOC not in task
    assert "METHODOLOGY" not in task


def test_task_never_contains_the_stage_code():
    task = render_generation_task(_DOC, _python_stage())
    assert "def transform" not in task
    assert _CODE not in task


def test_task_never_contains_existing_tests():
    stage = _python_stage()
    stage = parse_stage({**stage.model_dump(by_alias=True, exclude_none=True),
        "tests": [{"name": "stale_case",
                   "inputs": {"load": [{"amount": 1.0}]},
                   "expected": [{"amount": 1.0, "doubled": 2.0}]}]})
    task = render_generation_task(_DOC, stage)
    assert "stale_case" not in task


def test_stated_corner_cases_are_rendered_with_their_expected_outcome():
    """Both halves must reach the generator: a case with no stated outcome is one it
    would have to invent."""
    task = render_generation_task(_DOC, _python_stage(corner_cases=[
        {"case": "`amount` is blank", "expected": "the step fails"},
        {"case": "`amount` is negative", "expected": "the row is kept unchanged"},
    ]))
    assert "`amount` is blank" in task
    assert "the step fails" in task
    assert "`amount` is negative" in task
    assert "the row is kept unchanged" in task


def test_no_corner_cases_still_renders_a_task():
    """Declaring none is legal — the generator still has to find edge cases itself,
    it just has none stated for it."""
    task = render_generation_task(_DOC, _python_stage(corner_cases=[]))
    assert _SUMMARY in task
    assert "corner case" not in task.lower()


def test_a_stage_with_no_summary_cannot_generate_examples():
    """There is no description to check the code against, so generating anything
    would make the panel's 'checked against the code' claim untrue."""
    with pytest.raises(ValueError, match="has no summary"):
        render_generation_task(_DOC, _python_stage(summary=None))


def test_generator_rejects_non_python_stages():
    bad = parse_stage({
        "id": "pub", "name": "Publish", "type": "publish",
        "inputs": [{"id": "double", "schema": {"columns": [
            {"name": "amount", "type": "float", "nullable": False},
            {"name": "doubled", "type": "float", "nullable": False},
        ]}}],
        "function": {"kind": "inline", "code": "def transform(df, output_dir):\n    return df\n"},
        "publish": {},
    })
    with pytest.raises(ValueError, match="can run them"):
        build_stage_test_generator(_DOC, bad)


def test_generator_target_schema_is_stage_bound():
    agent = build_stage_test_generator(_DOC, _python_stage())
    with pytest.raises(Exception, match="declared inputs"):
        agent._target_schema.model_validate({"tests": [{
            "name": "x", "inputs": {"ghost": [{"amount": 1.0}]},
            "expected": [{"amount": 1.0, "doubled": 2.0}]}]})

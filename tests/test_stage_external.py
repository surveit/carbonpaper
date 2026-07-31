"""The `external` stage type: row-grained like python_row_function, and the ONE
type that runs a separate program — an argv named by its `external:` block,
spawned once per row, which no python stage can express.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest
from pydantic import ValidationError

from app.models import Stage, StageType, parse_stage
from app.models.stage import is_grain_and_order_preserving
from app.models.stages.external import NOT_REPRODUCIBLE_NOTE, ExternalStage
from app.runtime.preview import PREVIEWABLE_TYPES
from app.runtime.stages import HANDLERS, RowMapHandler
from app.runtime.stages.external import make_external_row_mapper
from conftest import make_run_context

_X_COLUMN = [{"name": "x", "type": "int"}]
_SCRIPT = str(Path(__file__).parent / "fixtures" / "external_row_script.py")


def _command(mode: str = "double") -> list[str]:
    return [sys.executable, _SCRIPT, mode]


def _external(external=None, inputs=("src",)) -> Stage:
    return parse_stage({
        "id": "capture", "name": "capture", "type": "external",
        "inputs": [{"id": i, "schema": {"columns": _X_COLUMN}} for i in inputs],
        "output_schema": {"columns": _X_COLUMN},
        "external": external or {"command": _command(), "timeout_seconds": 30},
    })


def _run(stage, frame):
    return HANDLERS[stage.type].execute(stage, {"src": frame}, make_run_context())


# ── the type is registered everywhere a stage type must be ───────────────────


def test_external_is_a_stage_type_whose_only_block_is_external_with_a_runtime_handler():
    # No `function:` field on the model: this type cannot express code at all.
    assert StageType.external.value == "external"
    assert ExternalStage.model_fields["external"].is_required()
    assert "function" not in ExternalStage.model_fields
    assert StageType.external in HANDLERS


def test_external_is_row_mapped_by_the_runtime_with_its_own_subprocess_mapper():
    handler = HANDLERS[StageType.external]
    assert isinstance(handler, RowMapHandler)
    assert handler.make_mapper is make_external_row_mapper
    assert handler.drops_rows is False


def test_external_is_grain_and_order_preserving_because_the_runtime_maps_it_per_row():
    # Positional alignment only: one process per input row, so output row i came
    # from input row i. NOT a purity claim — the VALUES may differ on a re-run.
    assert is_grain_and_order_preserving(StageType.external)
    assert HANDLERS[StageType.external].preserves_grain_and_order


# ── running it: one process per row, JSON in, JSON out ───────────────────────


def test_the_rows_the_command_writes_come_back_merged_in_input_order():
    out = _run(_external(), pd.DataFrame({"x": [1, 2, 3]}))
    assert list(out["x"]) == [2, 4, 6]


def test_n_rows_in_is_n_rows_out_in_order():
    frame = pd.DataFrame({"x": list(range(6))})
    out = _run(_external(), frame)
    assert len(out) == len(frame)
    assert list(out["x"]) == [value * 2 for value in frame["x"]]


def test_a_non_zero_exit_fails_loudly_naming_the_stage_and_the_row():
    stage = _external({"command": _command("fail"), "timeout_seconds": 30})
    with pytest.raises(RuntimeError) as err:
        _run(stage, pd.DataFrame({"x": [1]}))
    assert "capture" in str(err.value) and "row 0" in str(err.value)
    assert "exited 3" in str(err.value)
    assert "the browser never started" in str(err.value)  # the child's own stderr


def test_a_command_that_outlasts_its_timeout_is_killed_and_fails_loudly():
    stage = _external({"command": _command("hang"), "timeout_seconds": 1})
    with pytest.raises(RuntimeError) as err:
        _run(stage, pd.DataFrame({"x": [1]}))
    assert "capture" in str(err.value) and "row 0" in str(err.value)
    assert "timeout_seconds=1" in str(err.value)
    assert "killed" in str(err.value)


def test_stdout_that_is_not_json_fails_rather_than_yielding_a_partial_row():
    stage = _external({"command": _command("garbage"), "timeout_seconds": 30})
    with pytest.raises(RuntimeError, match="not one JSON object"):
        _run(stage, pd.DataFrame({"x": [1]}))


def test_json_that_is_not_an_object_is_not_a_row():
    stage = _external({"command": _command("not_an_object"), "timeout_seconds": 30})
    with pytest.raises(RuntimeError, match="not one object"):
        _run(stage, pd.DataFrame({"x": [1]}))


def test_external_takes_exactly_one_input():
    # Arity is declarative (`max_length=1` on `inputs`), so a second input is a
    # `too_long` error on the field rather than a hand-written message.
    with pytest.raises(ValidationError) as err:
        _external(inputs=("a", "b"))
    assert [(e["loc"], e["type"]) for e in err.value.errors()] == [
        (("external", "inputs"), "too_long")
    ]


# ── the command: what the model can actually check, checked when it is saved ─


def test_an_external_stage_carries_the_command_and_the_timeout_and_no_function_block():
    stage = _external()
    assert stage.external is not None
    assert stage.external.command == _command()
    assert stage.external.timeout_seconds == 30
    assert not hasattr(stage, "function")  # no `function:` block at all


def test_an_external_stage_must_carry_an_external_block():
    with pytest.raises(ValidationError, match="Field required"):
        parse_stage({
            "id": "t", "name": "t", "type": "external",
            "inputs": [{"id": "src", "schema": {"columns": _X_COLUMN}}],
            "output_schema": {"columns": _X_COLUMN},
        })


def test_a_program_that_resolves_nowhere_is_refused_by_name():
    with pytest.raises(ValidationError) as err:
        _external({"command": ["no-such-program-anywhere"], "timeout_seconds": 30})
    assert "no-such-program-anywhere" in str(err.value)
    assert "findable on PATH" in str(err.value)


def test_an_empty_command_is_refused():
    with pytest.raises(ValidationError):
        _external({"command": [], "timeout_seconds": 30})


def test_an_empty_argv_element_is_refused():
    with pytest.raises(ValidationError, match="empty argv element"):
        _external({"command": [sys.executable, ""], "timeout_seconds": 30})


def test_a_missing_timeout_is_refused_rather_than_defaulted():
    # No safe default exists for code that reaches outside, so none is invented.
    with pytest.raises(ValidationError, match="timeout_seconds"):
        _external({"command": _command()})


@pytest.mark.parametrize("timeout", [0, -1])
def test_a_non_positive_timeout_is_refused(timeout):
    with pytest.raises(ValidationError):
        _external({"command": _command(), "timeout_seconds": timeout})


def test_the_command_and_the_timeout_both_determine_what_the_stage_computes():
    base = _external()
    assert (
        _external({"command": _command("fail"), "timeout_seconds": 30})
        .compute_definition_fingerprint() != base.compute_definition_fingerprint()
    )
    assert (
        _external({"command": _command(), "timeout_seconds": 31})
        .compute_definition_fingerprint() != base.compute_definition_fingerprint()
    )


def test_the_not_reproducible_note_says_so_in_both_words():
    assert "not reproducible" in NOT_REPRODUCIBLE_NOTE.lower().replace("neither", "not")
    assert "reviewable" in NOT_REPRODUCIBLE_NOTE


# ── nothing may claim an external stage is checkable or previewable ──────────


def test_an_external_stage_carries_no_runnable_examples():
    """No authored example can pin a step whose output came from outside the run."""
    assert ExternalStage.CARRIES_RUNNABLE_TESTS is False


def test_authored_examples_are_refused_on_an_external_stage():
    with pytest.raises(ValidationError, match="tests"):
        parse_stage({
            "id": "capture", "name": "capture", "type": "external",
            "inputs": [{"id": "src", "schema": {"columns": _X_COLUMN}}],
            "output_schema": {"columns": _X_COLUMN},
            "external": {"command": _command(), "timeout_seconds": 30},
            "tests": [{
                "name": "case",
                "inputs": {"src": [{"x": 1}]},
                "expected": [{"x": 1}],
            }],
        })


def test_an_external_stage_is_not_previewable():
    # A scratch re-run would start the program — the one thing preview forbids.
    assert "external" not in PREVIEWABLE_TYPES


def test_the_stage_panel_states_the_command_and_the_standing_caveat():
    # Argv, timeout, and the standing caveat — nothing else about its reach,
    # which nothing here can check.
    from markupsafe import escape

    from app.web.config import templates

    html = templates.env.get_template("_stage_executable.html").render(
        stage=_external(), function_code=None
    )
    assert _SCRIPT in html
    assert "30" in html
    assert str(escape(NOT_REPRODUCIBLE_NOTE)) in html

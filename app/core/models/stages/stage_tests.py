"""StageTest — one authored input→expected-output case for a python transform
(python_row_function / python_frame_function) — plus the shape checks the Stage
model runs when it carries tests.

A test is a claim about what the stage's function must do, authored from the
methodology, never produced by executing the stage's own code (that would assert
the code equals itself). The runner that holds the code to these claims is
app.runtime.stage_tests; this module is only the data shape and its invariants.
Schema conformance of the rows needs dataframes, so it is checked by the runner,
not here.
"""
from __future__ import annotations

from typing import Any, Optional

from app.core.models.schema import _Base

# The stage types whose handlers can execute a test.
STAGE_TEST_TYPES = frozenset({"python_row_function", "python_frame_function"})


class StageTest(_Base):
    """One case: `inputs` maps each of the stage's declared upstream ids to that
    input's rows; `expected` is the output rows those inputs must produce.
    `name` is the case's stable handle — what it pins down, e.g.
    "withdrawn_bill_maps_to_null"; `description` says why the case exists."""
    name: str
    description: Optional[str] = None
    inputs: dict[str, list[dict[str, Any]]]
    expected: list[dict[str, Any]]


def validate_stage_tests(
    stage_type: str, input_ids: list[str], tests: list[StageTest]
) -> None:
    """Raise ValueError if `tests` are malformed for a stage of `stage_type`
    with the declared `input_ids`: tests belong on python transforms only,
    names are non-empty and unique, each test supplies exactly the declared
    inputs, and a python_row_function test is one row in → one row out (the
    type is 1:1 by construction, so a test claiming otherwise is wrong)."""
    if not tests:
        return
    if stage_type not in STAGE_TEST_TYPES:
        raise ValueError(
            f"tests are only supported on python transforms, not `{stage_type}`"
        )
    names = [test.name for test in tests]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise ValueError(f"duplicate test name(s): {duplicates}")
    declared = set(input_ids)
    for test in tests:
        if not test.name.strip():
            raise ValueError("a test needs a non-empty name")
        if set(test.inputs) != declared:
            raise ValueError(
                f"test {test.name!r}: inputs keys {sorted(test.inputs)} "
                f"must be exactly the stage's declared inputs {sorted(declared)}"
            )
        # Exhaustive over STAGE_TEST_TYPES (membership is checked above): the
        # fallthrough fires only when the set grows without the new type's
        # per-type invariant being decided here.
        match stage_type:
            case "python_row_function":
                input_rows = next(iter(test.inputs.values()), [])
                if len(input_rows) != 1 or len(test.expected) != 1:
                    raise ValueError(
                        f"test {test.name!r}: a python_row_function test is "
                        f"one row in → one row out (got {len(input_rows)} in, "
                        f"{len(test.expected)} out)"
                    )
            case "python_frame_function":
                pass  # no per-type invariant: rows in and rows out are both free
            case _:
                raise AssertionError(f"unhandled stage test type: {stage_type}")

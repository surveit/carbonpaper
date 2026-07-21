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

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, model_validator

from app.core.models.schema import _Base

# The stage types whose handlers can execute a test.
STAGE_TEST_TYPES = frozenset({"python_row_function", "python_frame_function"})

# The one origin value the generation pipeline stamps onto the tests it authors.
# A case WITHOUT this marker (origin is None) is human territory — hand-authored,
# hand-edited, or a fixture — and `stage_tests_are_frozen` keeps such a set out of
# a regenerate's reach.
GENERATED_ORIGIN = "generated"


class StageTest(_Base):
    """One case: `inputs` maps each of the stage's declared upstream ids to that
    input's rows; `expected` is the output rows those inputs must produce.
    `name` is the case's stable handle — what it pins down, e.g.
    "withdrawn_bill_maps_to_null"; `description` says why the case exists.

    `origin` is `"generated"` on a case the generation pipeline authored, and
    None (the default, dropped from the canonical dump) on any human-authored or
    hand-edited case — the marker `stage_tests_are_frozen` reads to decide whether
    a regenerate may overwrite a stage's suite."""
    name: str
    description: Optional[str] = None
    inputs: dict[str, list[dict[str, Any]]]
    expected: list[dict[str, Any]]
    origin: Optional[Literal["generated"]] = None


def stage_tests_are_frozen(tests: Optional[list[StageTest]]) -> bool:
    """Is this stage's test set HUMAN-touched, so a regenerate must leave it be?

    True iff the stage carries tests AND at least one case is NOT machine-authored
    (its `origin` is not "generated"): a hand-written case, a fixture, or a set a
    human edited. A set the generation pipeline wrote wholesale (every case
    `origin="generated"`) is False — safe to re-derive — and a stage with no tests
    is False (nothing to freeze)."""
    if not tests:
        return False
    return any(test.origin != GENERATED_ORIGIN for test in tests)


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


def build_stage_tests_model(
    stage_type: str, input_ids: list[str]
) -> type[BaseModel]:
    """A pydantic model of shape ``{"tests": [StageTest, ...]}`` whose
    validation is bound to one stage's context: the shape rules that
    validate_stage_tests enforces (inputs match the declared upstream ids,
    row functions are one row in → one row out) run at model_validate time.
    Built per stage so an agent's submit_answer tool can reject a malformed
    suite inside the agent loop instead of at stage-write time."""

    class StageTestSuite(BaseModel):
        tests: list[StageTest]

        model_config = ConfigDict(extra="forbid")

        @model_validator(mode="after")
        def _stage_rules(self) -> "StageTestSuite":
            validate_stage_tests(stage_type, input_ids, self.tests)
            return self

    return StageTestSuite

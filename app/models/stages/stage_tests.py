"""StageTest — one authored input→expected-output case for a python transform, plus the
shape and row validation the Stage model runs when it carries tests.
A test is authored from the methodology, never produced by executing the stage's own code."""
from __future__ import annotations

from typing import Any, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    ValidationError,
    model_serializer,
    model_validator,
)

from app.core.utils import format_errors
from app.models.schema import TableSchema, _Base


class StageTest(_Base):
    """A rows case states `expected` rows; a failure case states `expected: null`."""
    name: str
    description: Optional[str] = None
    inputs: dict[str, list[dict[str, Any]]]
    expected: Optional[list[dict[str, Any]]] = Field(
        description=(
            "The rows this input must produce — or null to claim the step must FAIL on "
            "it, refusing to hand back a value it cannot stand behind. Null and [] are "
            "different claims and never interchangeable: [] says the step succeeds and "
            "returns no rows, null says it does not succeed at all. Always state one of "
            "them explicitly; there is no default."
        ),
    )

    @model_serializer(mode="wrap")
    def _keep_a_failure_claim_visible(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, Any]:
        """`expected: null` survives an exclude_none dump — dropping the key would
        reload as a missing required field, and read as a forgotten one."""
        data = handler(self)
        data.setdefault("expected", None)
        return data


def validate_stage_tests(
    stage_type: str, input_ids: list[str], tests: list[StageTest]
) -> None:
    """Raise ValueError if `tests` are malformed for a stage of `stage_type` with the
    declared `input_ids`: names non-empty and unique, each test supplying exactly the
    declared inputs, and the per-type arity the `match` below spells out. WHETHER
    `stage_type` may carry tests at all is the caller's gate
    (StageBase.CARRIES_RUNNABLE_TESTS)."""
    if not tests:
        return
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
        # Exhaustive over the types declaring CARRIES_RUNNABLE_TESTS: the
        # fallthrough fires the moment a stage class flips that on without its
        # arity being decided here.
        match stage_type:
            case "python_row_function":
                _validate_row_function_row_counts(test)
            case "filter_rows":
                _validate_filter_row_counts(test)
            case "python_frame_function":
                pass  # no per-type invariant: rows in and rows out are both free
            case _:
                raise AssertionError(f"unhandled stage test type: {stage_type}")


def validate_test_rows(
    input_schemas: dict[str, TableSchema],
    output_schema: TableSchema,
    tests: list[StageTest],
) -> None:
    """Raise ValueError if any test row fails the schema it claims to instance.
    Judged through TableSchema.to_pydantic_model, so every declared column must be
    on EVERY row (a nullable one as an explicit None) and no other key is allowed —
    stricter than the runtime's stage-I/O validation, which only warns on an
    undeclared column: a real stage may pass extras through, but a test row
    inventing one states the wrong shape. Assumes validate_stage_tests passed."""
    input_models = {
        input_id: schema.to_pydantic_model(f"{input_id}_row")
        for input_id, schema in input_schemas.items()
    }
    expected_model = output_schema.to_pydantic_model("expected_row")
    problems = [
        problem
        for test in tests
        for problem in _find_test_row_problems(test, input_models, expected_model)
    ]
    if problems:
        raise ValueError("; ".join(problems))


def build_stage_tests_model(
    stage_type: str,
    input_schemas: dict[str, TableSchema],
    output_schema: TableSchema,
) -> type[BaseModel]:
    """A pydantic model of shape ``{"tests": [StageTest, ...]}`` whose
    validation is bound to one stage's context: the shape rules
    validate_stage_tests enforces (inputs match the declared upstream ids,
    row functions are one row in → one row out) and the row conformance
    validate_test_rows enforces both run at model_validate time. Built per
    stage so an agent's submit_answer tool can reject a malformed suite inside
    the agent loop instead of at stage-write time."""

    class StageTestSuite(BaseModel):
        tests: list[StageTest]

        model_config = ConfigDict(extra="forbid")

        @model_validator(mode="after")
        def _stage_rules(self) -> "StageTestSuite":
            validate_stage_tests(stage_type, list(input_schemas), self.tests)
            validate_test_rows(input_schemas, output_schema, self.tests)
            return self

    return StageTestSuite


def _validate_row_function_row_counts(test: StageTest) -> None:
    input_rows = len(next(iter(test.inputs.values()), []))
    if input_rows != 1:
        raise ValueError(
            f"test {test.name!r}: a python_row_function test is one row in "
            f"(got {input_rows} in)"
        )
    # A failure case (expected is None) claims no output rows at all, so only a
    # rows case has an output count left to hold.
    if test.expected is not None and len(test.expected) != 1:
        raise ValueError(
            f"test {test.name!r}: a python_row_function test is one row in → "
            f"one row out (got 1 in, {len(test.expected)} out)"
        )


def _validate_filter_row_counts(test: StageTest) -> None:
    input_rows = len(next(iter(test.inputs.values()), []))
    if input_rows != 1:
        raise ValueError(
            f"test {test.name!r}: a filter_rows test is one row in "
            f"(got {input_rows} in)"
        )
    # A failure case (expected is None) claims no output rows at all. A rows case
    # states the kept row ([row]) or the drop ([]) — never more than it was given.
    if test.expected is not None and len(test.expected) > 1:
        raise ValueError(
            f"test {test.name!r}: a filter_rows test is one row in → that row or "
            f"nothing (got 1 in, {len(test.expected)} out)"
        )


def _find_test_row_problems(
    test: StageTest,
    input_models: dict[str, type[BaseModel]],
    expected_model: type[BaseModel],
) -> list[str]:
    problems = [
        f"test {test.name!r}, input {input_id!r}: {problem}"
        for input_id, row_model in input_models.items()
        for problem in _find_row_problems(test.inputs[input_id], row_model)
    ]
    if test.expected is None:
        return problems  # a failure case claims no output rows to conform
    problems += [
        f"test {test.name!r}, expected rows: {problem}"
        for problem in _find_row_problems(test.expected, expected_model)
    ]
    return problems


def _find_row_problems(
    rows: list[dict[str, Any]], row_model: type[BaseModel]
) -> list[str]:
    problems: list[str] = []
    for index, row in enumerate(rows):
        try:
            row_model.model_validate(row)
        except ValidationError as err:
            problems += [f"row {index}: {issue}" for issue in format_errors(err)]
    return problems

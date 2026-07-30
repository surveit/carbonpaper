"""StageTest — one authored input→expected-output case for a python transform, plus the
shape and row validation the Stage model runs when it carries tests.
A test is authored from the methodology, never produced by executing the stage's own code."""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from app.core.utils import format_errors
from app.models.schema import TableSchema, _Base

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


def build_stage_tests_model_for_stage(stage: Any) -> type[BaseModel]:
    """`build_stage_tests_model` bound to one `Stage`, narrowed to its declared
    read-set when it has one — so an agent authoring cases for a stage that reads
    2 of 16 columns is required to write 2-column rows, not 16-column ones."""
    from app.models.stages.inner import scoped_row_schemas

    scoped = scoped_row_schemas(stage)
    if scoped is None:
        return build_stage_tests_model(
            stage.type,
            {ref.id: ref.table_schema for ref in stage.inputs},
            stage.output_schema,
        )
    input_row, expected_row = scoped
    return build_stage_tests_model(
        stage.type,
        {ref.id: input_row for ref in stage.inputs},
        expected_row,
    )


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

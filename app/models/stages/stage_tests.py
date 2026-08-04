"""StageTest — one authored input→expected-output case for a python transform, and the
per-stage-type subclasses whose `expected` type IS that type's output arity.
A test is authored from the methodology, never produced by executing the stage's own code."""
from __future__ import annotations

from typing import Annotated, Any, Optional, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    ValidationError,
    create_model,
    model_serializer,
    model_validator,
)

import pandas as pd

from app.core.frame_checks import find_frame_violations, find_primary_key_violations
from app.core.utils import format_errors
from app.models.schema import StageId, TableSchema, _Base

# One row: column name → cell value. WHICH columns is not knowable here — it comes
# from the stage's own declared schema, checked at validate_test_rows — so this is a
# dynamic boundary, not a model waiting to be written.
DataRow: TypeAlias = dict[str, Any]

_INPUTS_DESCRIPTION = (
    "Rows fed to the stage, keyed by the upstream stage id they come from — exactly "
    "its declared input ids. Every row states every column, nulls explicit."
)
_EXPECTED_DESCRIPTION = (
    "The rows the step must produce, or null to claim it must FAIL rather than return "
    "a value it cannot stand behind. [] is not null: [] is success with no rows. "
    "State one; no default."
)

# Exactly one input holding exactly one row: what a step the runtime invokes per row
# is handed by a test of it.
_OneInputRow: TypeAlias = Annotated[
    dict[StageId, Annotated[list[DataRow], Field(min_length=1, max_length=1)]],
    Field(min_length=1, max_length=1),
]


class StageTest(_Base):
    """A rows case states `expected` rows; a failure case states `expected: null`."""
    name: str
    description: Optional[str] = None
    inputs: dict[StageId, list[DataRow]] = Field(description=_INPUTS_DESCRIPTION)
    expected: Optional[list[DataRow]] = Field(description=_EXPECTED_DESCRIPTION)

    @model_serializer(mode="wrap")
    def _keep_a_failure_claim_visible(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, Any]:
        """`expected: null` survives an exclude_none dump — dropping the key would
        reload as a missing required field, and read as a forgotten one."""
        data = handler(self)
        data.setdefault("expected", None)
        return data


class PythonRowFunctionStageTest(StageTest):
    """One row in → that one row out, or a refusal."""
    inputs: _OneInputRow = Field(description=_INPUTS_DESCRIPTION)
    expected: Optional[Annotated[list[DataRow], Field(min_length=1, max_length=1)]] = (
        Field(description=_EXPECTED_DESCRIPTION)
    )


class FilterRowsStageTest(StageTest):
    """One row in → that row kept, dropped (`[]`), or a refusal."""
    inputs: _OneInputRow = Field(description=_INPUTS_DESCRIPTION)
    expected: Optional[Annotated[list[DataRow], Field(max_length=1)]] = Field(
        description=_EXPECTED_DESCRIPTION
    )


class PythonFrameFunctionStageTest(StageTest):
    """Any rows in → any rows out, or a refusal: a frame function may reshape freely."""
    expected: Optional[list[DataRow]] = Field(description=_EXPECTED_DESCRIPTION)


def validate_stage_tests(input_ids: list[StageId], tests: list[StageTest]) -> None:
    """Raise ValueError unless names are non-empty and unique and each test supplies
    exactly `input_ids`. Per-type row arity is the StageTest subclass's job, and
    WHETHER a type may carry tests at all is the caller's
    (StageBase.CARRIES_RUNNABLE_TESTS)."""
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


def validate_test_rows(
    input_schemas: dict[StageId, TableSchema],
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


def validate_test_frames(
    input_schemas: dict[StageId, TableSchema],
    output_schema: TableSchema,
    tests: list[StageTest],
) -> None:
    """Raise ValueError if a test's rows break a cross-row rule a real run enforces."""
    # The row-by-row checks (validate_test_rows) cannot see these: a row is
    # well-formed on its own and the suite still states a frame no stage could
    # ever be handed. Assumes validate_stage_tests passed.
    problems = [
        problem
        for test in tests
        for problem in _find_test_frame_problems(test, input_schemas, output_schema)
    ]
    if problems:
        raise ValueError("; ".join(problems))


def build_stage_tests_model(
    test_class: type[StageTest],
    input_schemas: dict[StageId, TableSchema],
    output_schema: TableSchema,
) -> type[BaseModel]:
    """A ``{"tests": [test_class, ...]}`` model bound to one stage's inputs and output
    schema, so an agent's submit_answer rejects a malformed suite inside the agent
    loop rather than at stage-write time."""

    class StageTestSuite(BaseModel):
        tests: list[StageTest]

        model_config = ConfigDict(extra="forbid")

        @model_validator(mode="after")
        def _stage_rules(self) -> "StageTestSuite":
            validate_stage_tests(list(input_schemas), self.tests)
            validate_test_rows(input_schemas, output_schema, self.tests)
            validate_test_frames(input_schemas, output_schema, self.tests)
            return self

    # `list[test_class]` is a runtime type, so it enters through an Any-typed handle
    # rather than the static annotation grammar.
    element_type: Any = test_class
    return create_model(
        StageTestSuite.__name__, __base__=StageTestSuite, tests=(list[element_type], ...)
    )


def _find_test_row_problems(
    test: StageTest,
    input_models: dict[StageId, type[BaseModel]],
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


def _find_test_frame_problems(
    test: StageTest,
    input_schemas: dict[StageId, TableSchema],
    output_schema: TableSchema,
) -> list[str]:
    # An input frame must pass every rule the runner applies to a stage input
    # (key uniqueness AND no exact duplicate rows); the expected rows only the one
    # it applies to a stage output (key uniqueness). A test states frames a real
    # run would have to accept — no stricter, no looser.
    problems = [
        f"test {test.name!r}, input {input_id!r}: {violation.message}"
        for input_id, schema in input_schemas.items()
        for violation in find_frame_violations(
            pd.DataFrame(test.inputs[input_id]), primary_key=schema.primary_key
        )
    ]
    if test.expected is None:
        return problems  # a failure case claims no output rows to form a frame
    problems += [
        f"test {test.name!r}, expected rows: {violation.message}"
        for violation in find_primary_key_violations(
            pd.DataFrame(test.expected), output_schema.primary_key
        )
    ]
    return problems


def _find_row_problems(
    rows: list[DataRow], row_model: type[BaseModel]
) -> list[str]:
    problems: list[str] = []
    for index, row in enumerate(rows):
        try:
            row_model.model_validate(row)
        except ValidationError as err:
            problems += [f"row {index}: {issue}" for issue in format_errors(err)]
    return problems

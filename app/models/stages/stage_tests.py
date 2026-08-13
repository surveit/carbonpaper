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


from app.core.frame_checks import find_frame_violations
from app.core.utils import format_errors
from app.models.schema import StageId, TableSchema, _Base
from app.models.tool_schema_prompts import (
    FILTER_ROWS_STAGE_TEST_DESCRIPTION,
    PYTHON_FRAME_FUNCTION_STAGE_TEST_DESCRIPTION,
    PYTHON_ROW_FUNCTION_STAGE_TEST_DESCRIPTION,
    STAGE_TEST_DESCRIPTION,
    STARLARK_ROW_FUNCTION_STAGE_TEST_DESCRIPTION,
)

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


# `row` is a position in that run's output for `input`, narrowed to what the step reads.
class RowSelection(_Base):
    input: StageId
    run_id: str
    row: int = Field(ge=0)
    filter: str
    # How many rows the filter selected, out of how many it read. A case whose filter
    # matched most of the frame is grounded on a row that may exercise nothing.
    matched: int = Field(ge=1)
    scanned: int = Field(ge=1)


class StageTest(_Base):
    model_config = ConfigDict(json_schema_extra={"description": STAGE_TEST_DESCRIPTION})

    name: str
    description: Optional[str] = None
    inputs: dict[StageId, list[DataRow]] = Field(description=_INPUTS_DESCRIPTION)
    expected: Optional[list[DataRow]] = Field(description=_EXPECTED_DESCRIPTION)
    # Where each input row came from, in `inputs` order per input. Empty on a case whose
    # rows were written because no real row shows it; `authored_reason` then says why an
    # input like it could turn up later, which is the whole claim such a case makes.
    selections: list[RowSelection] = []
    authored_reason: Optional[str] = None

    @model_serializer(mode="wrap")
    def _keep_a_failure_claim_visible(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, Any]:
        """Without this, an exclude_none dump drops the key and the record fails to reload."""
        data = handler(self)
        data.setdefault("expected", None)
        return data


class PythonRowFunctionStageTest(StageTest):
    model_config = ConfigDict(json_schema_extra={"description": PYTHON_ROW_FUNCTION_STAGE_TEST_DESCRIPTION})

    inputs: _OneInputRow = Field(description=_INPUTS_DESCRIPTION)
    expected: Optional[Annotated[list[DataRow], Field(min_length=1, max_length=1)]] = (
        Field(description=_EXPECTED_DESCRIPTION)
    )


class StarlarkRowFunctionStageTest(StageTest):
    model_config = ConfigDict(json_schema_extra={"description": STARLARK_ROW_FUNCTION_STAGE_TEST_DESCRIPTION})

    inputs: _OneInputRow = Field(description=_INPUTS_DESCRIPTION)
    expected: Optional[Annotated[list[DataRow], Field(min_length=1, max_length=1)]] = (
        Field(description=_EXPECTED_DESCRIPTION)
    )


class FilterRowsStageTest(StageTest):
    model_config = ConfigDict(json_schema_extra={"description": FILTER_ROWS_STAGE_TEST_DESCRIPTION})

    inputs: _OneInputRow = Field(description=_INPUTS_DESCRIPTION)
    expected: Optional[Annotated[list[DataRow], Field(max_length=1)]] = Field(
        description=_EXPECTED_DESCRIPTION
    )


class PythonFrameFunctionStageTest(StageTest):
    model_config = ConfigDict(json_schema_extra={"description": PYTHON_FRAME_FUNCTION_STAGE_TEST_DESCRIPTION})

    expected: Optional[list[DataRow]] = Field(description=_EXPECTED_DESCRIPTION)


def validate_stage_tests(input_ids: list[StageId], tests: list[StageTest]) -> None:
    names =[test.name for test in tests]
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
        _refuse_inconsistent_provenance(test, declared)


def _refuse_inconsistent_provenance(test: StageTest, declared: set[StageId]) -> None:
    if test.selections and test.authored_reason:
        raise ValueError(
            f"test {test.name!r}: its rows are selected from real data, so it cannot "
            f"also state why they were written instead"
        )
    unknown = sorted({s.input for s in test.selections} - declared)
    if unknown:
        raise ValueError(
            f"test {test.name!r}: a row is selected from {unknown}, which this step "
            f"does not read — its inputs are {sorted(declared)}"
        )
    for input_id, rows in test.inputs.items():
        selected = [s for s in test.selections if s.input == input_id]
        if selected and len(selected) != len(rows):
            raise ValueError(
                f"test {test.name!r}, input {input_id!r}: {len(selected)} row(s) "
                f"selected for {len(rows)} row(s) fed in — each row states where it came from"
            )


def validate_test_rows(
    input_schemas: dict[StageId, TableSchema],
    output_schema: TableSchema,
    tests: list[StageTest],
) -> None:
    """Assumes validate_stage_tests passed. Stricter than the runtime, which only warns on extras."""
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
    """Assumes validate_stage_tests passed."""
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
    return [
        f"test {test.name!r}, input {input_id!r}: {violation.message}"
        for input_id in input_schemas
        for violation in find_frame_violations(test.inputs[input_id])
    ]


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

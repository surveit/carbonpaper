from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Generic, TypeGuard

from pydantic import BaseModel, ConfigDict, ValidationError

from evals.harness.definition import EvalDefinition, ExpectedT, InputT, OutputT


class Case(BaseModel, Generic[InputT, ExpectedT]):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    input: InputT
    expected_outputs: list[ExpectedT]


class Dataset(BaseModel, Generic[InputT, ExpectedT]):
    model_config = ConfigDict(extra="forbid")

    eval: str
    cases: list[Case[InputT, ExpectedT]]


class DatasetInvalid(Exception):
    pass


def read_dataset(
    path: Path, definition: EvalDefinition[InputT, ExpectedT, OutputT]
) -> Dataset[InputT, ExpectedT]:
    dataset_model = _parametrize_dataset(definition.input_model, definition.expected_model)
    try:
        dataset = dataset_model.model_validate_json(path.read_text(encoding="utf-8"))
    except ValidationError as error:
        raise DatasetInvalid(_join_problems(path, _describe_invalid_fields(error))) from error
    problems = _find_dataset_problems(dataset, definition.name)
    if problems:
        raise DatasetInvalid(_join_problems(path, problems))
    return dataset


def _parametrize_dataset(
    input_model: type[InputT], expected_model: type[ExpectedT]
) -> type[Dataset[InputT, ExpectedT]]:
    # mypy rejects Dataset[input_model, expected_model]; the guard verifies the class built here.
    parametrized = Dataset.__class_getitem__((input_model, expected_model))
    if not _is_dataset_of(parametrized, input_model, expected_model):
        raise TypeError(
            f"pydantic did not parametrize Dataset with {input_model.__name__} "
            f"and {expected_model.__name__}"
        )
    return parametrized


def _is_dataset_of(
    model: object, input_model: type[InputT], expected_model: type[ExpectedT]
) -> TypeGuard[type[Dataset[InputT, ExpectedT]]]:
    return (
        isinstance(model, type)
        and issubclass(model, Dataset)
        and model.__pydantic_generic_metadata__["args"] == (input_model, expected_model)
    )


def _describe_invalid_fields(error: ValidationError) -> list[str]:
    return [
        f"{'.'.join(str(part) for part in entry['loc'])}: {entry['msg']}"
        for entry in error.errors()
    ]


def _join_problems(path: Path, problems: list[str]) -> str:
    return f"{path}: {'; '.join(problems)}"


def _find_dataset_problems(dataset: Dataset[InputT, ExpectedT], eval_name: str) -> list[str]:
    problems: list[str] = []
    if dataset.eval != eval_name:
        problems.append(f"dataset is for eval {dataset.eval!r}, not {eval_name!r}")
    if not dataset.cases:
        problems.append("dataset has no cases")
    repeated_ids = _find_repeated(case.case_id for case in dataset.cases)
    problems += [f"case_id {case_id!r} is repeated" for case_id in repeated_ids]
    for case in dataset.cases:
        problems += _find_case_problems(case)
    return problems


def _find_case_problems(case: Case[InputT, ExpectedT]) -> list[str]:
    if not case.expected_outputs:
        return [f"case {case.case_id!r} has no expected outputs"]
    repeated_keys = _find_repeated(expected.key for expected in case.expected_outputs)
    return [
        f"case {case.case_id!r} lists expected key {key!r} more than once"
        for key in repeated_keys
    ]


def _find_repeated(values: Iterable[str]) -> list[str]:
    return [value for value, count in Counter(values).items() if count > 1]

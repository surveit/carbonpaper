from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Generic

from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError

from evals.harness.definition import EvalDefinition, ExpectedT, InputT, OutputT


@dataclass(frozen=True)
class Case(Generic[InputT, ExpectedT]):
    case_id: str
    input: InputT
    expected_outputs: list[ExpectedT]


@dataclass(frozen=True)
class Dataset(Generic[InputT, ExpectedT]):
    eval: str
    cases: list[Case[InputT, ExpectedT]]


class DatasetInvalid(Exception):
    pass


def read_dataset(
    path: Path, definition: EvalDefinition[InputT, ExpectedT, OutputT]
) -> Dataset[InputT, ExpectedT]:
    raw_dataset = _parse_raw_dataset(path)
    _validate_raw_dataset(raw_dataset, definition.name, path)
    cases = _read_cases(raw_dataset.cases, definition.input_model, definition.expected_model, path)
    return Dataset(eval=raw_dataset.eval, cases=cases)


class _RawCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    input: JsonValue
    expected_outputs: list[JsonValue]


class _RawDataset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    eval: str
    cases: list[_RawCase]


def _parse_raw_dataset(path: Path) -> _RawDataset:
    try:
        return _RawDataset.model_validate_json(path.read_text(encoding="utf-8"))
    except ValidationError as error:
        raise DatasetInvalid(_join_problems(path, _describe_errors("dataset", error))) from error


def _validate_raw_dataset(raw_dataset: _RawDataset, eval_name: str, path: Path) -> None:
    if raw_dataset.eval != eval_name:
        problem = f"dataset is for eval {raw_dataset.eval!r}, not {eval_name!r}"
        raise DatasetInvalid(_join_problems(path, [problem]))
    if not raw_dataset.cases:
        raise DatasetInvalid(_join_problems(path, ["dataset has no cases"]))
    repeated_ids = _find_repeated(raw_case.case_id for raw_case in raw_dataset.cases)
    if repeated_ids:
        problems = [f"case_id {case_id!r} is repeated" for case_id in repeated_ids]
        raise DatasetInvalid(_join_problems(path, problems))


def _read_cases(
    raw_cases: list[_RawCase],
    input_model: type[InputT],
    expected_model: type[ExpectedT],
    path: Path,
) -> list[Case[InputT, ExpectedT]]:
    cases: list[Case[InputT, ExpectedT]] = []
    problems: list[str] = []
    for raw_case in raw_cases:
        case_or_problems = _read_case(raw_case, input_model, expected_model)
        if isinstance(case_or_problems, Case):
            cases.append(case_or_problems)
        else:
            problems += case_or_problems
    if problems:
        raise DatasetInvalid(_join_problems(path, problems))
    return cases


def _read_case(
    raw_case: _RawCase, input_model: type[InputT], expected_model: type[ExpectedT]
) -> Case[InputT, ExpectedT] | list[str]:
    subject = f"case {raw_case.case_id!r}"
    expected_outputs, problems = _read_expected_outputs(raw_case, expected_model, subject)
    try:
        case_input = input_model.model_validate(raw_case.input)
    except ValidationError as error:
        return _describe_errors(f"{subject} input", error) + problems
    if problems:
        return problems
    return Case(case_id=raw_case.case_id, input=case_input, expected_outputs=expected_outputs)


def _read_expected_outputs(
    raw_case: _RawCase, expected_model: type[ExpectedT], subject: str
) -> tuple[list[ExpectedT], list[str]]:
    expected_outputs: list[ExpectedT] = []
    problems: list[str] = []
    for position, value in enumerate(raw_case.expected_outputs):
        try:
            expected_outputs.append(expected_model.model_validate(value))
        except ValidationError as error:
            problems += _describe_errors(f"{subject} expected_outputs[{position}]", error)
    if not raw_case.expected_outputs:
        problems.append(f"{subject} has no expected outputs")
    repeated_keys = _find_repeated(expected.key for expected in expected_outputs)
    problems += [f"{subject} lists expected key {key!r} more than once" for key in repeated_keys]
    return expected_outputs, problems


def _describe_errors(subject: str, error: ValidationError) -> list[str]:
    return [
        f"{subject}{_format_location(entry['loc'])}: {entry['msg']}" for entry in error.errors()
    ]


def _format_location(location: tuple[int | str, ...]) -> str:
    return "".join(f"[{part}]" if isinstance(part, int) else f".{part}" for part in location)


def _join_problems(path: Path, problems: list[str]) -> str:
    return f"{path}: {'; '.join(problems)}"


def _find_repeated(values: Iterable[str]) -> list[str]:
    return [value for value, count in Counter(values).items() if count > 1]

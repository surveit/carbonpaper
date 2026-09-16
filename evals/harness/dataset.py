from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Generic

from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError

from evals.harness.definition import EvalDefinition, ExpectedT, InputT, OutputT
from evals.harness.validation import (
    describe_errors,
    find_repeated,
    join_problems,
    read_as_json,
)

DATASET_FILE = "dataset.json"

_CASE_ID = re.compile(r"[a-z0-9][a-z0-9_-]*")
_CASE_ID_RULE = (
    "a case_id may contain only lowercase letters, digits, hyphens and underscores, "
    "and must start with a letter or digit"
)


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


class _RawDataset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    eval: str
    cases: list[JsonValue]


class _RawCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    input: JsonValue
    expected_outputs: list[JsonValue]


def _parse_raw_dataset(path: Path) -> _RawDataset:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise DatasetInvalid(join_problems(path, ["no dataset file"])) from error
    try:
        return _RawDataset.model_validate_json(text)
    except ValidationError as error:
        problems = describe_errors("dataset", (), error)
        raise DatasetInvalid(join_problems(path, problems)) from error


def _validate_raw_dataset(raw_dataset: _RawDataset, eval_name: str, path: Path) -> None:
    if raw_dataset.eval != eval_name:
        problem = f"dataset is for eval {raw_dataset.eval!r}, not {eval_name!r}"
        raise DatasetInvalid(join_problems(path, [problem]))
    if not raw_dataset.cases:
        raise DatasetInvalid(join_problems(path, ["dataset has no cases"]))
    readable_ids = [_find_readable_case_id(case_value) for case_value in raw_dataset.cases]
    repeated_ids = find_repeated(case_id for case_id in readable_ids if case_id is not None)
    if repeated_ids:
        problems = [f"case_id {case_id!r} is repeated" for case_id in repeated_ids]
        raise DatasetInvalid(join_problems(path, problems))


def _read_cases(
    case_values: list[JsonValue],
    input_model: type[InputT],
    expected_model: type[ExpectedT],
    path: Path,
) -> list[Case[InputT, ExpectedT]]:
    cases: list[Case[InputT, ExpectedT]] = []
    problems = _find_case_id_problems(case_values)
    for position, case_value in enumerate(case_values):
        case_or_problems = _read_case(position, case_value, input_model, expected_model)
        if isinstance(case_or_problems, Case):
            cases.append(case_or_problems)
        else:
            problems += case_or_problems
    if problems:
        raise DatasetInvalid(join_problems(path, problems))
    return cases


def _find_case_id_problems(case_values: list[JsonValue]) -> list[str]:
    readable_ids = [_find_readable_case_id(case_value) for case_value in case_values]
    refused_ids = [
        case_id
        for case_id in readable_ids
        if case_id is not None and not _CASE_ID.fullmatch(case_id)
    ]
    if not refused_ids:
        return []
    listed_ids = ", ".join(repr(case_id) for case_id in refused_ids)
    return [f"refused case_ids {listed_ids} ({_CASE_ID_RULE})"]


def _read_case(
    position: int,
    case_value: JsonValue,
    input_model: type[InputT],
    expected_model: type[ExpectedT],
) -> Case[InputT, ExpectedT] | list[str]:
    subject = _name_case(position, case_value)
    try:
        raw_case = read_as_json(_RawCase, case_value)
    except ValidationError as error:
        return describe_errors(subject, (), error)
    expected_outputs, problems = _read_expected_outputs(raw_case, expected_model, subject)
    try:
        case_input = read_as_json(input_model, raw_case.input)
    except ValidationError as error:
        return describe_errors(subject, ("input",), error) + problems
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
            expected_outputs.append(read_as_json(expected_model, value))
        except ValidationError as error:
            problems += describe_errors(subject, ("expected_outputs", position), error)
    if not raw_case.expected_outputs:
        problems.append(f"{subject} has no expected outputs")
    repeated_keys = find_repeated(expected.key for expected in expected_outputs)
    problems += [f"{subject} lists expected key {key!r} more than once" for key in repeated_keys]
    return expected_outputs, problems


def _name_case(position: int, case_value: JsonValue) -> str:
    case_id = _find_readable_case_id(case_value)
    return f"cases[{position}]" if case_id is None else f"case {case_id!r}"


def _find_readable_case_id(case_value: JsonValue) -> str | None:
    if not isinstance(case_value, dict):
        return None
    case_id = case_value.get("case_id")
    return case_id if isinstance(case_id, str) else None

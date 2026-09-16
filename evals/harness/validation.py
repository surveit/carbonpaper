from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, JsonValue, ValidationError

ModelT = TypeVar("ModelT", bound=BaseModel)


def validate_as_json(model: type[ModelT], value: JsonValue) -> ModelT:
    return model.model_validate_json(json.dumps(value))


def describe_errors(
    subject: str, prefix: tuple[int | str, ...], error: ValidationError
) -> list[str]:
    return [
        _describe_error(subject, (*prefix, *entry["loc"]), entry["msg"]) for entry in error.errors()
    ]


def join_problems(path: Path, problems: list[str]) -> str:
    return f"{path}: {'; '.join(problems)}"


def find_repeated(values: Iterable[str]) -> list[str]:
    return [value for value, count in Counter(values).items() if count > 1]


def inflect(count: int, noun: str) -> str:
    return noun if count == 1 else f"{noun}s"


def _describe_error(subject: str, location: tuple[int | str, ...], message: str) -> str:
    location_text = _format_location(location)
    return f"{subject} {location_text}: {message}" if location_text else f"{subject}: {message}"


def _format_location(location: tuple[int | str, ...]) -> str:
    parts = "".join(f"[{part}]" if isinstance(part, int) else f".{part}" for part in location)
    return parts.removeprefix(".")

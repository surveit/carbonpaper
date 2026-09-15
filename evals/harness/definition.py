from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict


class ExpectedOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str


InputT = TypeVar("InputT", bound=BaseModel)
ExpectedT = TypeVar("ExpectedT", bound=ExpectedOutput)
OutputT = TypeVar("OutputT", bound=BaseModel)


@dataclass(frozen=True)
class EvalDefinition(Generic[InputT, ExpectedT, OutputT]):
    name: str
    input_model: type[InputT]
    expected_model: type[ExpectedT]
    output_model: type[OutputT]
    outcomes: tuple[str, ...]
    default_repeats: int
    load: Callable[[InputT, LoadContext], Loaded[OutputT]]
    judge: Callable[[OutputT, list[ExpectedT]], list[Judgement]]


class Loaded(BaseModel, Generic[OutputT]):
    model_config = ConfigDict(extra="forbid")

    output: OutputT
    cost_usd: float | None


@dataclass(frozen=True)
class LoadContext:
    pass_dir: Path
    workspace_dir: Path
    repo_root: Path


class Judgement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    outcome: str
    note: str


class CaseRefused(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason

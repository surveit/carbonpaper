from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Generic, Protocol

from evals.harness import orchestration, report, rulings
from evals.harness.definition import EvalDefinition, ExpectedT, InputT, OutputT
from evals.harness.rulings import Disagreement


class ResolvedEval(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def default_repeats(self) -> int: ...

    def run_pass(
        self,
        *,
        eval_dir: Path,
        repeats: int,
        confirmed_loads: int,
        case_ids: list[str],
        repo_root: Path,
    ) -> Path: ...

    def judge_pass(self, pass_dir: Path, *, eval_dir: Path) -> None: ...

    def write_report(self, pass_dir: Path, *, eval_dir: Path, against: Path | None) -> None: ...

    def find_ruling_disagreements(self, rulings_path: Path) -> list[Disagreement]: ...


class EvalNotFound(Exception):
    pass


def resolve_eval_by_name(name: str) -> ResolvedEval:
    module = _import_eval_module(name)
    if not hasattr(module, _EVAL_ATTRIBUTE):
        raise EvalNotFound(f"no eval named {name!r}: evals.{name} defines no {_EVAL_ATTRIBUTE}")
    definition = getattr(module, _EVAL_ATTRIBUTE)
    if not isinstance(definition, EvalDefinition):
        raise EvalNotFound(
            f"no eval named {name!r}: evals.{name}.{_EVAL_ATTRIBUTE} is a "
            f"{type(definition).__name__}, not an EvalDefinition"
        )
    return bind_eval(definition)


def bind_eval(definition: EvalDefinition[InputT, ExpectedT, OutputT]) -> ResolvedEval:
    return BoundEval(definition)


@dataclass(frozen=True)
class BoundEval(Generic[InputT, ExpectedT, OutputT]):
    definition: EvalDefinition[InputT, ExpectedT, OutputT]

    @property
    def name(self) -> str:
        return self.definition.name

    @property
    def default_repeats(self) -> int:
        return self.definition.default_repeats

    def run_pass(
        self,
        *,
        eval_dir: Path,
        repeats: int,
        confirmed_loads: int,
        case_ids: list[str],
        repo_root: Path,
    ) -> Path:
        return orchestration.run_pass(
            self.definition,
            eval_dir=eval_dir,
            repeats=repeats,
            confirmed_loads=confirmed_loads,
            case_ids=case_ids,
            repo_root=repo_root,
        )

    def judge_pass(self, pass_dir: Path, *, eval_dir: Path) -> None:
        orchestration.judge_pass(self.definition, pass_dir, eval_dir=eval_dir)

    def write_report(self, pass_dir: Path, *, eval_dir: Path, against: Path | None) -> None:
        built = report.build_pass_report(
            self.definition, pass_dir, eval_dir=eval_dir, against=against
        )
        report.write_pass_report(built, pass_dir)

    def find_ruling_disagreements(self, rulings_path: Path) -> list[Disagreement]:
        return rulings.find_ruling_disagreements(self.definition, rulings_path)


def _import_eval_module(name: str) -> ModuleType:
    module_name = f"evals.{name}"
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError as error:
        if error.name != module_name:
            raise
        raise EvalNotFound(f"no eval named {name!r}: no module {module_name}") from error


_EVAL_ATTRIBUTE = "EVAL"

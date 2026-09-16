from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from evals.harness.definition import Judgement, OutputT
from evals.harness.validation import describe_errors, inflect, join_problems


class OutputStored(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["stored"] = "stored"
    case_id: str
    attempt: int
    seconds: float
    cost_usd: float | None


class LoadRefused(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["refused"] = "refused"
    case_id: str
    attempt: int
    seconds: float
    reason: str


LoadRecord = Annotated[OutputStored | LoadRefused, Field(discriminator="kind")]


class PassRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    eval: str
    pass_id: str
    code_commit: str
    repeats: int
    case_ids: list[str]
    started_at: str
    loads: list[LoadRecord]


class JudgementRefused(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str


JudgementFile = list[Judgement] | JudgementRefused


class PassFolderExists(Exception):
    pass


class PassIncomplete(Exception):
    pass


class PassRecordUnreadable(Exception):
    pass


@dataclass(frozen=True)
class UnaccountedCase:
    case_id: str
    missing_attempts: tuple[int, ...]


def find_unaccounted_cases(record: PassRecord) -> list[UnaccountedCase]:
    cases = [
        UnaccountedCase(case_id=case_id, missing_attempts=_find_missing_attempts(record, case_id))
        for case_id in record.case_ids
    ]
    return [case for case in cases if case.missing_attempts]


def validate_pass_is_complete(record: PassRecord, pass_dir: Path) -> None:
    unaccounted = find_unaccounted_cases(record)
    if unaccounted:
        listed = "; ".join(_describe_unaccounted_case(case) for case in unaccounted)
        raise PassIncomplete(f"{pass_dir}: loading stopped before recording {listed}")


@dataclass(frozen=True)
class JudgedAttempt:
    case_id: str
    attempt: int
    judgements: JudgementFile


@dataclass(frozen=True)
class PassFolder:
    root: Path

    @classmethod
    def create(cls, eval_dir: Path, pass_id: str) -> PassFolder:
        passes_dir = eval_dir / "passes"
        passes_dir.mkdir(parents=True, exist_ok=True)
        folder = cls(passes_dir / pass_id)
        try:
            folder.root.mkdir()
        except FileExistsError as error:
            raise PassFolderExists(f"pass folder {folder.root} already exists") from error
        folder.workspace_dir.mkdir()
        return folder

    @property
    def workspace_dir(self) -> Path:
        return self.root / "workspace"

    def write_record(self, record: PassRecord) -> None:
        partial_path = self.root / "pass.json.partial"
        partial_path.write_text(record.model_dump_json(indent=2), encoding="utf-8")
        os.replace(partial_path, self._record_path)

    def read_record(self) -> PassRecord:
        try:
            text = self._record_path.read_text(encoding="utf-8")
        except FileNotFoundError as error:
            raise PassRecordUnreadable(
                join_problems(self._record_path, ["no pass record"])
            ) from error
        try:
            return PassRecord.model_validate_json(text)
        except ValidationError as error:
            problems = describe_errors("pass", (), error)
            raise PassRecordUnreadable(join_problems(self._record_path, problems)) from error

    def write_output(self, case_id: str, attempt: int, output: BaseModel) -> None:
        path = self._output_path(case_id, attempt)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(output.model_dump_json(indent=2), encoding="utf-8")

    def read_output(self, case_id: str, attempt: int, output_model: type[OutputT]) -> OutputT:
        path = self._output_path(case_id, attempt)
        return output_model.model_validate_json(path.read_text(encoding="utf-8"))

    def replace_judgements(self, judged_attempts: list[JudgedAttempt]) -> None:
        if self._judgements_dir.exists():
            shutil.rmtree(self._judgements_dir)
        self._judgements_dir.mkdir()
        for judged in judged_attempts:
            path = self._judgement_path(judged.case_id, judged.attempt)
            path.parent.mkdir(exist_ok=True)
            content = _JUDGEMENT_FILE.dump_json(judged.judgements, indent=2).decode("utf-8")
            path.write_text(content, encoding="utf-8")

    def find_judgement(self, case_id: str, attempt: int) -> JudgementFile | None:
        path = self._judgement_path(case_id, attempt)
        if not path.exists():
            return None
        return _JUDGEMENT_FILE.validate_json(path.read_text(encoding="utf-8"))

    @property
    def _record_path(self) -> Path:
        return self.root / "pass.json"

    @property
    def _judgements_dir(self) -> Path:
        return self.root / "judgements"

    def _output_path(self, case_id: str, attempt: int) -> Path:
        return self.root / "outputs" / case_id / f"{attempt}.json"

    def _judgement_path(self, case_id: str, attempt: int) -> Path:
        return self._judgements_dir / case_id / f"{attempt}.json"


def _describe_unaccounted_case(case: UnaccountedCase) -> str:
    attempts = ", ".join(str(attempt) for attempt in case.missing_attempts)
    noun = inflect(len(case.missing_attempts), "attempt")
    return f"case {case.case_id!r} {noun} {attempts}"


def _find_missing_attempts(record: PassRecord, case_id: str) -> tuple[int, ...]:
    loads = [load for load in record.loads if load.case_id == case_id]
    refused_attempts = [load.attempt for load in loads if isinstance(load, LoadRefused)]
    last_attempt = min(refused_attempts) if refused_attempts else record.repeats
    recorded_attempts = {load.attempt for load in loads}
    return tuple(
        attempt for attempt in range(1, last_attempt + 1) if attempt not in recorded_attempts
    )


_JUDGEMENT_FILE: TypeAdapter[JudgementFile] = TypeAdapter(JudgementFile)

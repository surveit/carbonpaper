from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from evals.harness.definition import Judgement, OutputT


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
    reason: str


LoadRecord = Annotated[OutputStored | LoadRefused, Field(discriminator="kind")]


class PassRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    eval: str
    pass_id: str
    code_commit: str
    repeats: int
    started_at: str
    loads: list[LoadRecord]


class JudgementRefused(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str


class PassFolderExists(Exception):
    pass


@dataclass(frozen=True)
class JudgedAttempt:
    case_id: str
    attempt: int
    judgements: list[Judgement] | JudgementRefused


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
        self._record_path.write_text(record.model_dump_json(indent=2), encoding="utf-8")

    def read_record(self) -> PassRecord:
        return PassRecord.model_validate_json(self._record_path.read_text(encoding="utf-8"))

    def write_output(self, case_id: str, attempt: int, output: BaseModel) -> None:
        path = self._output_path(case_id, attempt)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(output.model_dump_json(indent=2), encoding="utf-8")

    def read_output(self, case_id: str, attempt: int, output_model: type[OutputT]) -> OutputT:
        path = self._output_path(case_id, attempt)
        return output_model.model_validate_json(path.read_text(encoding="utf-8"))

    def replace_judgements(self, judged_attempts: list[JudgedAttempt]) -> None:
        judgements_dir = self.root / "judgements"
        if judgements_dir.exists():
            shutil.rmtree(judgements_dir)
        judgements_dir.mkdir()
        for judged in judged_attempts:
            path = judgements_dir / judged.case_id / f"{judged.attempt}.json"
            path.parent.mkdir(exist_ok=True)
            content = _JUDGEMENT_FILE.dump_json(judged.judgements, indent=2).decode("utf-8")
            path.write_text(content, encoding="utf-8")

    @property
    def _record_path(self) -> Path:
        return self.root / "pass.json"

    def _output_path(self, case_id: str, attempt: int) -> Path:
        return self.root / "outputs" / case_id / f"{attempt}.json"


_JUDGEMENT_FILE: TypeAdapter[list[Judgement] | JudgementRefused] = TypeAdapter(
    list[Judgement] | JudgementRefused
)

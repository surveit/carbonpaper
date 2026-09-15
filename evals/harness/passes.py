from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


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


class PassFolderExists(Exception):
    pass


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

    def write_output(self, case_id: str, attempt: int, output: BaseModel) -> None:
        path = self._output_path(case_id, attempt)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(output.model_dump_json(indent=2), encoding="utf-8")

    @property
    def _record_path(self) -> Path:
        return self.root / "pass.json"

    def _output_path(self, case_id: str, attempt: int) -> Path:
        return self.root / "outputs" / case_id / f"{attempt}.json"

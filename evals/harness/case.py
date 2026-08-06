"""One eval case: where the golden came from, the brief a blind build gets, and the
contract for comparing a build's output against the golden."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator

# A cell of a golden table. WHICH columns a row holds comes from the case's own
# `golden.columns`, so the mapping is case-defined — the same dynamic boundary as
# app.models.stages.stage_tests.DataRow. The value type is closed, unlike the columns.
CellValue: TypeAlias = Optional[str | int | float]
GoldenRow: TypeAlias = dict[str, CellValue]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=False)


class SourceRef(_Strict):
    repo: str
    commit: str = Field(description="Full SHA. The golden is read at THIS commit, never HEAD.")
    notebook_path: str
    cell_index: int = Field(description="Index among the notebook's CODE cells, not all cells.")
    article_url: Optional[str] = None


class InputFile(_Strict):
    path: str = Field(description="Path within the source repo at `commit`.")
    sha256: str


class GoldenTable(_Strict):
    key_column: str
    columns: list[str] = Field(min_length=1, description="Every column, key included.")
    rows: list[GoldenRow] = Field(min_length=1)

    @model_validator(mode="after")
    def _rows_state_every_column(self) -> "GoldenTable":
        if self.key_column not in self.columns:
            raise ValueError(f"key_column {self.key_column!r} is not among columns")
        declared = set(self.columns)
        for row in self.rows:
            if set(row) != declared:
                raise ValueError(
                    f"golden row {row.get(self.key_column)!r} states {sorted(row)}, "
                    f"not the declared columns {sorted(declared)}"
                )
        keys = [row[self.key_column] for row in self.rows]
        if len(set(keys)) != len(keys):
            raise ValueError("golden rows are not unique on key_column")
        return self


class ComparisonContract(_Strict):
    """How a build's output is lined up with the golden. Every column the brief declares
    matches by name; only the key may be renamed, and only to close a leak."""

    output_key_column: str = Field(
        description=(
            "The key column's name in a build's output. Differs from the golden's only "
            "where the golden's own name would reveal which frame to join from."
        )
    )
    compared_columns: dict[str, str] = Field(
        min_length=1, description="Golden column name -> output column name."
    )
    tolerance: float = Field(ge=0.0, description="Absolute tolerance for numeric cells.")


class Case(_Strict):
    case_id: str
    source: SourceRef
    inputs: list[InputFile] = Field(min_length=1)
    brief: str = Field(
        description=(
            "What a blind build is given, with the input files: the story to tell and the "
            "output schema. Never the method, and never written from the notebook."
        )
    )
    golden: GoldenTable
    comparison: ComparisonContract
    curation_notes: list[str] = Field(
        default_factory=list,
        description="Judgement calls made while curating — what a later reader cannot recover.",
    )

    @model_validator(mode="after")
    def _contract_covers_the_golden(self) -> "Case":
        unknown = sorted(set(self.comparison.compared_columns) - set(self.golden.columns))
        if unknown:
            raise ValueError(f"compared_columns names golden column(s) that do not exist: {unknown}")
        if self.golden.key_column not in self.comparison.compared_columns:
            raise ValueError(
                f"compared_columns must map the key column {self.golden.key_column!r}"
            )
        return self


def load_case(path: Path) -> Case:
    return Case.model_validate(json.loads(path.read_text(encoding="utf-8")))


def write_case(path: Path, case: Case) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(case.model_dump_json(indent=2, exclude_none=True) + "\n", encoding="utf-8")

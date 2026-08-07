"""One eval case: where the golden came from, the brief a blind build gets, and the golden
table itself. Comparison is positional, so the brief owns the sort order and there is no key."""
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
    path: str = Field(description="The notebook, or a committed output file, holding the golden.")
    cell_index: Optional[int] = Field(
        default=None,
        description="For a notebook golden: index among the CODE cells. None for a data file.",
    )
    article_url: Optional[str] = None


class InputFile(_Strict):
    path: str = Field(description="Path within the source repo at `commit`.")
    sha256: str


class GoldenTable(_Strict):
    """Rows in the order the source produced them — the order IS part of the answer."""

    columns: list[str] = Field(min_length=1)
    rows: list[GoldenRow] = Field(min_length=1)

    @model_validator(mode="after")
    def _rows_state_every_column(self) -> "GoldenTable":
        declared = set(self.columns)
        for position, row in enumerate(self.rows):
            if set(row) != declared:
                raise ValueError(
                    f"golden row {position} states {sorted(row)}, not the declared "
                    f"columns {sorted(declared)}"
                )
        return self


class Case(_Strict):
    case_id: str
    source: SourceRef
    inputs: list[InputFile] = Field(min_length=1)
    brief: str = Field(
        description=(
            "What a blind build is given, with the input files: the story to tell, the output "
            "schema, and the SORT ORDER including tie-breaks — comparison is positional, so an "
            "under-specified sort makes a build disagree for no reason. Never the method, and "
            "never written from the notebook."
        )
    )
    golden: GoldenTable
    tolerance: float = Field(
        ge=0.0,
        description=(
            "Numeric tolerance, RELATIVE above 1.0 and absolute below it. A golden cell is "
            "rendered to fixed significant figures, so a column printed as 2.073536e+08 can "
            "never be compared to an absolute 1e-6."
        ),
    )
    curation_notes: list[str] = Field(
        default_factory=list,
        description="Judgement calls made while curating — what a later reader cannot recover.",
    )


def load_case(path: Path) -> Case:
    return Case.model_validate(json.loads(path.read_text(encoding="utf-8")))


def write_case(path: Path, case: Case) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(case.model_dump_json(indent=2, exclude_none=True) + "\n", encoding="utf-8")

"""Where a claimed or published value sits in a run, and what a publish stage said about it."""
from __future__ import annotations

from typing import Annotated, Literal, Self, Union

from pydantic import (
    BaseModel, Field, SerializeAsAny, TypeAdapter, field_validator, model_validator,
)

from app.core.figure_text import render_figure
from app.core.json_types import JsonScalar
from app.core.ids import ID
from app.models.locators import PageCharRange, parse_any_locator
from app.models.spans import Span, read_span_cell


class CitedValue(BaseModel):
    stage_id: ID
    row_ordinal: int
    column: str
    label: str  # what the artifact calls this value
    value: str  # the cell, checked against the row before this was recorded


class Citation(BaseModel):
    """Where the evidence sits. The kind decides what else a citation carries."""

    kind: str


class StageOutputCellCitation(Citation):
    kind: Literal["stage_output_cell"] = "stage_output_cell"
    run_id: ID = Field(description="The run, as the evidence pool names it.")
    stage_id: ID = Field(description="The stage, as the evidence pool names it.")
    row_ordinal: int = Field(description="The row's position in the stage output, counting from 0.")
    column: str = Field(description="The column's name, spelled as the evidence pool spells it.")
    value: JsonScalar = Field(description="The cell's value, copied exactly as the evidence pool prints it.")


# A cell holding a span: the cell's address in the run, then where its quote sits in the file.
class SourceSpanCitation(Citation):
    kind: Literal["source_span"] = "source_span"
    run_id: ID = Field(description="The run, as the evidence pool names it.")
    stage_id: ID = Field(description="The stage, as the evidence pool names it.")
    row_ordinal: int = Field(description="The row's position in the stage output, counting from 0.")
    column: str = Field(description="The column's name, spelled as the evidence pool spells it.")
    source_id: ID = Field(description="The file the quote is from, copied exactly.")
    source_sha256: str = Field(description="The file's sha256, copied exactly.")
    locator: SerializeAsAny[PageCharRange] = Field(
        description="The page the quote is on and its characters there, copied exactly.")
    quote: str = Field(description="The quote, copied exactly as the evidence pool prints it.")

    @field_validator("locator", mode="before")
    @classmethod
    def _parse_locator_by_kind(cls, value: object) -> object:
        return parse_any_locator(value)

    @model_validator(mode="after")
    def _names_a_span(self) -> Self:
        self.build_span()
        return self

    def build_span(self) -> Span:
        return Span(source_id=self.source_id, source_sha256=self.source_sha256,
                    locator=self.locator, quote=self.quote)

    def is_held_in(self, cell: object) -> bool:
        # A citation carries no prefix or suffix: the locator alone places the quote.
        span = self.build_span()
        return any(held.model_copy(update={"prefix": None, "suffix": None}) == span
                   for held in read_span_cell(cell) or [])


# A figure: one cell of a run, holding a value or a span.
CellCitation = StageOutputCellCitation | SourceSpanCitation


class RowsRectangle(BaseModel):
    """A table inside a stage's output: rows [row_start, row_end) by name-ordered columns."""

    row_start: int
    row_end: int
    columns: list[str]

    def count_rows(self) -> int:
        return self.row_end - self.row_start

    def is_whole_output(self, columns: list[str], row_count: int) -> bool:
        # By set: a reordering cuts no cell, and the question here is what was cut.
        return (
            self.row_start == 0
            and self.row_end == row_count
            and set(self.columns) == set(columns)
        )


class StageOutputTableCitation(Citation):
    # The rows and columns actually published, never the whole frame by assumption.
    kind: Literal["stage_output_table"] = "stage_output_table"
    run_id: ID
    stage_id: ID
    rectangle: RowsRectangle


PublishedCitation = Annotated[
    Union[StageOutputCellCitation, SourceSpanCitation, StageOutputTableCitation],
    Field(discriminator="kind"),
]


def render_citation_value(citation: PublishedCitation) -> str:
    if isinstance(citation, StageOutputCellCitation):
        return render_figure(citation.value)
    if isinstance(citation, SourceSpanCitation):
        return citation.quote
    # A table names rows, not one cell; its row count is the fact it carries.
    return f"{citation.rectangle.count_rows():,} rows"


class StageOutputRowCitation(Citation):
    # A row pointed at with no value of its own — the show-the-work link.
    kind: Literal["stage_output_row"] = "stage_output_row"
    stage_id: ID
    row_ordinal: int


class StageOutputColumnCitation(Citation):
    kind: Literal["stage_output_column"] = "stage_output_column"
    run_id: ID = Field(description="The run whose output holds the column.")
    stage_id: ID = Field(description="The stage, as the evidence pool names it.")
    column: str = Field(description="The column's name, spelled as the evidence pool spells it.")


class StageCitation(Citation):
    kind: Literal["stage"] = "stage"
    stage_id: ID = Field(description="The stage, as the evidence pool names it.")


class TermCitation(Citation):
    kind: Literal["term"] = "term"
    name: str = Field(description="The defined term, exactly as the terms name it.")


ChallengeCitation = Annotated[
    Union[StageOutputCellCitation, SourceSpanCitation, StageOutputColumnCitation, StageCitation,
          TermCitation],
    Field(discriminator="kind"),
]


# ── the same five, stamped with the project when a review is stored ──
class AddressedStageOutputCellCitation(StageOutputCellCitation):
    project_id: ID


class AddressedSourceSpanCitation(SourceSpanCitation):
    project_id: ID


class AddressedStageOutputColumnCitation(StageOutputColumnCitation):
    project_id: ID


class AddressedStageCitation(StageCitation):
    project_id: ID


class AddressedTermCitation(TermCitation):
    project_id: ID


AddressedChallengeCitation = Annotated[
    Union[AddressedStageOutputCellCitation, AddressedSourceSpanCitation,
          AddressedStageOutputColumnCitation, AddressedStageCitation, AddressedTermCitation],
    Field(discriminator="kind"),
]

ADDRESSED_CHALLENGE_CITATION: TypeAdapter[AddressedChallengeCitation] = TypeAdapter(
    AddressedChallengeCitation)


def address_citation(project_id: ID, citation: ChallengeCitation) -> AddressedChallengeCitation:
    stamped = {**citation.model_dump(), "project_id": project_id}
    return ADDRESSED_CHALLENGE_CITATION.validate_python(stamped)

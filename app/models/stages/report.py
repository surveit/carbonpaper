"""The report stage: its config block, and `one_file_per` resolving against the input."""
from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, ClassVar, Literal, Optional, Sequence

from pydantic import ConfigDict, Field

from app.models.schema import StageConfig
from app.models.stages.stage_base import StageInput, StageType
from app.models.stages.code import (
    CarriesPythonFunctionStage,
)
from app.models.stages.shared import COLUMN_ISSUE, resolve_input_columns
from app.models.stages.stage_type_spec import StageTypeSpec
from app.models.stages.signature import ReplacesSignature
from app.models.tool_schema_prompts import REPORT_CONFIG_DESCRIPTION

if TYPE_CHECKING:
    from app.models.workflow_stage import WorkflowStageInput


class ReportFormat(str, Enum):
    html_report = "html_report"
    json = "json"
    csv = "csv"
    evidence_cards = "evidence_cards"


class ReportConfig(StageConfig):
    model_config = ConfigDict(json_schema_extra={"description": REPORT_CONFIG_DESCRIPTION})

    FINGERPRINT_FIELDS: ClassVar[frozenset[str]] = frozenset({
        "format", "destination", "one_file_per", "cross_link",
    })
    INCIDENTAL_FIELDS: ClassVar[frozenset[str]] = frozenset()

    format: Optional[ReportFormat] = None
    destination: Optional[str] = None
    one_file_per: Optional[str] = None
    cross_link: Optional[bool] = None


class ReportStage(CarriesPythonFunctionStage):
    # The one type that emits files rather than a table.
    REQUIRES_OUTPUT_SCHEMA: ClassVar[bool] = False

    type: Literal[StageType.report]
    CACHE_IGNORED_BECAUSE: ClassVar[str] = (
        "report writes the artifacts a reader opens, and a replayed frame would skip the write"
    )
    report: ReportConfig
    inputs: list[StageInput] = Field(default_factory=list, min_length=1)
    signature: ReplacesSignature

    def fingerprint_blocks(self) -> dict[str, StageConfig]:
        return {"report": self.report, **super().fingerprint_blocks()}

    def find_config_column_issues(
        self, inputs: Sequence["WorkflowStageInput"]
    ) -> list[str]:
        return find_report_column_issues(self, inputs)

    def find_signature_config_issues(self) -> list[str]:
        signature = self.signature
        if signature.produces:
            return [
                f"stage '{self.id}': report emits files, not a table — "
                f"signature produces must be empty"
            ]
        return []


def find_report_column_issues(
    stage: "ReportStage", inputs: Sequence["WorkflowStageInput"]
) -> list[str]:
    report = stage.report
    if not report.one_file_per:
        return []
    cols = resolve_input_columns(inputs, 0)
    if report.one_file_per in cols:
        return []
    return [
        COLUMN_ISSUE.format(
            sid=stage.id, field="report.one_file_per", col=report.one_file_per, cols=sorted(cols)
        )
    ]

# Authoring copy for this module's stage type(s); assembled into STAGE_TYPES.
STAGE_TYPE_SPECS: dict[str, StageTypeSpec] = {
    "report": StageTypeSpec(
        summary="Render a final artifact (html, json, csv, cards).",
        signature_form="replaces",
        blocks=["report", "function"],
        requires_inputs=True,
        min_inputs=1,
        required=[],
        optional=["format", "destination", "one_file_per", "cross_link",
                  "summary"],
        notes=(
            "A workflow need not have one — a run whose result is a table is finished without it.\n"
            "Reported output must be INTERROGABLE: every figure and every row it "
            "renders says where it came from. Declare the keyword `citation_provider` on "
            "the function — `def transform(df, output_dir, citation_provider)` — and the "
            "runtime hands it a provider over this stage's own input frames.\n"
            "A FIGURE IS CITED: you assert the value and where it came from, the provider "
            "checks that cell really holds it, and you get back the row's trace URL — "
            "`url = citation_provider.cite_value(\"count_filings\", 0, \"filings\", "
            "filings, label=\"Filings in scope\")`. Pass the CELL, not the text you "
            "typeset from it: `4461000.0`, never `\"$4,461,000\"`. Print the figure and "
            "the link together, in one cell or one line, so a reader meets the number and "
            "its source at once. A citation that names a value the cell does not hold "
            "STOPS THE RUN, because report renders what a stage computed — arithmetic "
            "over two cells belongs in an aggregate or starlark step ahead of report, "
            "where the result becomes a cell that can be cited like any other.\n"
            "A TABLE ROW with no figure of its own uses "
            "`citation_provider.cite_row(\"<input stage id>\", row_ordinal)`, which claims "
            "a row and no value. row_ordinal is that row's 0-based position in the input "
            "frame AS RECEIVED: iterate the frame in order (enumerate it) and do not sort, "
            "filter or dedup first — position is the only key a citation has. A stage may "
            "only cite its own inputs. Omit the keyword for a format that cannot carry a "
            "link (csv, json).\n"
            "Nothing reads the artifact back, so printing a number other than the one "
            "cited beside it is NOT caught. "
            "The one type whose signature produces nothing: it emits files, not a table."
        ),
    ),
}

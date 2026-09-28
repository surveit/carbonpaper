"""read_pages stage: the config block, and the checks its input and signature must pass."""
from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Literal, Sequence

from pydantic import Field

from app.models.schema import SPAN_COLUMN_TYPE, STR_COLUMN_TYPE, StageConfig, TableSchema
from app.models.stages.shared import (
    COLUMN_ISSUE,
    find_declared_vs_computed_issues,
    resolve_input_columns,
)
from app.models.stages.signature import ReplacesSignature
from app.models.stages.stage_base import AbstractStage, StageInput, StageType
from app.models.stages.stage_type_spec import StageTypeSpec

if TYPE_CHECKING:
    from app.models.workflow_stage import WorkflowStageInput

SOURCE_ID_COLUMN = "source_id"
SOURCE_SHA256_COLUMN = "source_sha256"
PAGE_COLUMN = "page"
PAGE_TEXT_COLUMN = "page_text"
PAGE_SPAN_COLUMN = "page_span"

# The two input columns that name one stored file, copied onto each of its page rows.
SOURCE_COLUMN_TYPES: dict[str, str] = {
    SOURCE_ID_COLUMN: STR_COLUMN_TYPE,
    SOURCE_SHA256_COLUMN: STR_COLUMN_TYPE,
}
PAGE_COLUMN_TYPES: dict[str, str] = {
    PAGE_COLUMN: "int",
    PAGE_TEXT_COLUMN: STR_COLUMN_TYPE,
    PAGE_SPAN_COLUMN: SPAN_COLUMN_TYPE,
}


class ReadPagesConfig(StageConfig):
    FINGERPRINT_FIELDS: ClassVar[frozenset[str]] = frozenset({"carry"})
    INCIDENTAL_FIELDS: ClassVar[frozenset[str]] = frozenset()

    carry: list[str] = Field(
        description=(
            "Columns of each file's row to copy onto every one of its page rows, such as "
            "the docket entry and the date filed. Empty carries nothing. `source_id` and "
            "`source_sha256` are copied whatever this says, so never list them here."
        )
    )


class ReadPagesStage(AbstractStage):
    type: Literal[StageType.read_pages]
    CACHE_IGNORED_BECAUSE: ClassVar[str] = (
        "the cache stores a table, not the lineage sidecar this type works out, so a hit would replay the rows without their provenance"
    )
    read_pages: ReadPagesConfig
    # Exactly one input: the table naming the files, one row each.
    inputs: list[StageInput] = Field(default_factory=list, min_length=1, max_length=1)
    signature: ReplacesSignature

    def fingerprint_blocks(self) -> dict[str, StageConfig]:
        return {"read_pages": self.read_pages}

    def find_signature_config_issues(self) -> list[str]:
        return find_read_pages_carry_issues(self)

    def find_config_column_issues(
        self, inputs: Sequence["WorkflowStageInput"]
    ) -> list[str]:
        return find_read_pages_column_issues(self, inputs)

    def find_signature_schema_issues(
        self, inputs: Sequence["WorkflowStageInput"]
    ) -> list[str]:
        return find_read_pages_signature_issues(self, inputs)


def find_read_pages_carry_issues(stage: "ReadPagesStage") -> list[str]:
    written = {**SOURCE_COLUMN_TYPES, **PAGE_COLUMN_TYPES}
    return [
        f"stage '{stage.id}': read_pages.carry names `{name}`, which read_pages writes "
        f"itself — leave it out of carry"
        for name in stage.read_pages.carry
        if name in written
    ]


def find_read_pages_column_issues(
    stage: "ReadPagesStage", inputs: Sequence["WorkflowStageInput"]
) -> list[str]:
    cols = resolve_input_columns(inputs, 0)
    issues = [
        COLUMN_ISSUE.format(sid=stage.id, field=field, col=name, cols=sorted(cols))
        for field, names in (("read_pages", SOURCE_COLUMN_TYPES),
                             ("read_pages.carry", stage.read_pages.carry))
        for name in names
        if name not in cols
    ]
    supplied = inputs[0].table_schema
    issues.extend(
        f"stage '{stage.id}': read_pages reads `{name}` as {expected!r}, but its input "
        f"declares it {column.type!r}"
        for name, expected in SOURCE_COLUMN_TYPES.items()
        if (column := supplied.column_for_name(name)) is not None and column.type != expected
    )
    return issues


def find_read_pages_signature_issues(
    stage: "ReadPagesStage", inputs: Sequence["WorkflowStageInput"]
) -> list[str]:
    return [
        *_find_read_issues(stage, inputs[0].id),
        *_find_produces_issues(stage, inputs[0].table_schema),
    ]


def _find_read_issues(stage: "ReadPagesStage", input_id: str) -> list[str]:
    declared = {
        column.name
        for entry in stage.signature.reads
        if entry.input == input_id
        for column in entry.columns
    }
    consumed = {*SOURCE_COLUMN_TYPES, *stage.read_pages.carry}
    issues = [
        f"stage '{stage.id}': signature reads `{name}` but read_pages never consumes it"
        for name in sorted(declared - consumed)
    ]
    issues.extend(
        f"stage '{stage.id}': read_pages consumes `{name}` but the signature does not read it"
        for name in sorted(consumed - declared)
    )
    return issues


def _find_produces_issues(stage: "ReadPagesStage", edge: TableSchema) -> list[str]:
    produces = stage.signature.produces
    computed = _compute_read_pages_output_types(stage.read_pages, edge)
    issues = find_declared_vs_computed_issues(
        stage.id, "read_pages signature", TableSchema(columns=produces), computed)
    produced = {column.name for column in produces}
    issues.extend(
        f"stage '{stage.id}': read_pages writes `{name}` but the signature's produces omits it"
        for name in sorted(set(computed) - produced)
    )
    return issues


def _compute_read_pages_output_types(
    config: ReadPagesConfig, edge: TableSchema
) -> dict[str, str | None]:
    carried: dict[str, str | None] = {}
    for name in config.carry:
        column = edge.column_for_name(name)
        carried[name] = column.type if column is not None else None
    return {**SOURCE_COLUMN_TYPES, **carried, **PAGE_COLUMN_TYPES}


# Authoring copy for this module's stage type(s); assembled into STAGE_TYPES.
STAGE_TYPE_SPECS: dict[str, StageTypeSpec] = {
    "read_pages": StageTypeSpec(
        summary="Read every page of each stored file its input names: one row per page, "
                "holding the page's text and a span that quotes all of it.",
        signature_form="replaces",
        blocks=["read_pages"],
        requires_inputs=True,
        min_inputs=1,
        required=["carry"],
        optional=[],
        notes=(
            "This is how a document becomes rows a model can read and a reviewer can check "
            "against the page. Takes exactly ONE input with one row per stored file: "
            "`source_id` (str, the stored file's id) and `source_sha256` (str, what its "
            "bytes hash to). A PDF is read page by page; a .txt, .md or .html file is one "
            "page.\n"
            "The signature READS `source_id`, `source_sha256` and every carried column. It "
            "PRODUCES exactly the carried columns (each with its input type) plus "
            "`source_id` (str), `source_sha256` (str), `page` (int, counting from 1), "
            "`page_text` (str, the page's text as read) and `page_span` (span, quoting the "
            "whole page). Every other input column is DROPPED, so carry what a later step "
            "needs.\n"
            "An llm_transform that quotes a page reads `page_span` and names it as the "
            "`quoted_from` of the span column it adds. A page with no text layer, such as "
            "a scan, gets empty `page_text` and a warning naming the file. A `source_id` "
            "the project does not hold, or bytes "
            "that no longer hash to `source_sha256`, stop the run.\n"
            "Name what one output row is in `row_type_id`: a page of the input's kind of "
            "document, such as `filing_page`."
        ),
    ),
}

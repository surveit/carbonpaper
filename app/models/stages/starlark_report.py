"""starlark_report stage: the sandboxed counterpart of report."""
from __future__ import annotations

from typing import ClassVar, Literal, Optional

import starlark
from pydantic import Field, model_validator

from app.core.starlark_source import (
    DEFAULT_FUNCTION_NAME,
    REFUSE_BUILTIN,
    compile_starlark_module,
    find_bound_function,
)
from app.models.schema import StageConfig
from app.models.stages.stage_base import AbstractStage, StageInput, StageType
from app.models.stages.code import CORNER_CASES_DESCRIPTION, SUMMARY_DESCRIPTION, CornerCase
from app.models.stages.stage_type_spec import StageTypeSpec
from app.models.stages.signature import ReplacesSignature
from app.models.stages.starlark import (
    STARLARK_LANGUAGE_NOTE,
    VALUE_MARSHALLING_NOTE,
)
from app.models.stages.warnings import CompilerWarning, warn

EMIT_FILE_BUILTIN = "emit_file"
EMIT_TABLE_BUILTIN = "emit_table"
ESCAPE_BUILTIN = "escape"
FORMAT_NUMBER_BUILTIN = "format_number"
CITE_VALUE_BUILTIN = "cite_value"
CITE_ROW_BUILTIN = "cite_row"

# Starlark resolves free names statically, so these bind at validation and execution alike.
REPORT_BUILTINS: tuple[str, ...] = (
    EMIT_FILE_BUILTIN, EMIT_TABLE_BUILTIN, ESCAPE_BUILTIN, FORMAT_NUMBER_BUILTIN,
    CITE_VALUE_BUILTIN, CITE_ROW_BUILTIN,
)

BUILTIN_SURFACE_NOTE = (
    "Everything this step can write, it writes through these builtins — no import needed, "
    "and there is nothing else: no filesystem, no network, no libraries.\n"
    f"`{EMIT_FILE_BUILTIN}(name, text)` writes one file into this stage's output directory. "
    "`name` is a plain relative filename; one that climbs out of the directory is refused.\n"
    f"`{EMIT_TABLE_BUILTIN}(name, rows)` writes a list of row dicts as CSV, taking its "
    "columns from the first row — a later row carrying different keys stops the run rather "
    "than being written under the wrong header.\n"
    f"`{ESCAPE_BUILTIN}(text)` is HTML escaping. Put every cell you place inside markup "
    "through it.\n"
    f"`{FORMAT_NUMBER_BUILTIN}(value, decimals=2, thousands_separator=True)` is how a number "
    "reaches the page. Starlark's own `%s` renders 10333414.94 as `1.033341e+07`, so a "
    "figure printed that way is not the figure that was cited — always put a number "
    f"through {FORMAT_NUMBER_BUILTIN}, and handle a blank cell yourself before you do: it "
    "refuses None rather than choosing what an absent figure should read as.\n"
    f"`{CITE_VALUE_BUILTIN}(input_id, row_ordinal, column, value, label=...)` asserts that "
    "cell holds that value and returns the row's trace URL. Pass the CELL, not the text you "
    "typeset from it: `4461000.0`, never `\"$4,461,000\"`. A value the cell does not hold "
    "STOPS THE RUN, because a report renders what a stage computed — arithmetic over two "
    "cells belongs in an aggregate or starlark step ahead of the report, where the result "
    "becomes a cell that can be cited like any other.\n"
    f"`{CITE_ROW_BUILTIN}(input_id, row_ordinal)` claims a row and no value, returning that "
    "row's trace URL. `row_ordinal` is the row's 0-based position in the input list AS "
    "RECEIVED: enumerate it and do not sort, filter or dedup first, because position is the "
    "only key a citation has. A stage may only cite its own inputs."
)

_WORKED_EXAMPLE = (
    "def transform(totals, filings):\n"
    "    total = totals[0][\"total_usd\"]\n"
    "    total_url = cite_value(\"sum_spend\", 0, \"total_usd\", total, label=\"Total spend\")\n"
    "    lines = []\n"
    "    for i, filing in enumerate(filings):\n"
    "        row_url = cite_row(\"scored_filings\", i)\n"
    "        lines.append('<p><a href=\"%s\">%s</a></p>'\n"
    "                     % (row_url, escape(filing[\"client\"])))\n"
    "    emit_file(\"report.html\",\n"
    "              '<h1><a href=\"%s\">$%s</a></h1>%s'\n"
    "              % (total_url, format_number(total), \"\\n\".join(lines)))\n"
    "    emit_table(\"filings.csv\", filings)\n"
)


_CODE_DESCRIPTION = (
    "Inline Starlark defining `function` (default `transform`). It takes one POSITIONAL "
    "parameter per declared input, in `inputs` order — each a list of row dicts in the "
    "order that input produced them — and returns nothing: what the step writes is what it "
    "emitted.\n"
    + BUILTIN_SURFACE_NOTE + "\n"
    + STARLARK_LANGUAGE_NOTE + " " + VALUE_MARSHALLING_NOTE + "\n"
    "Worked example, over an aggregate input and a row-level one:\n"
    + _WORKED_EXAMPLE
)

_FUNCTION_DESCRIPTION = (
    f"Name of the function to call within `code`, defaulting to `{DEFAULT_FUNCTION_NAME}`. "
    "Set it only when the function is not called that."
)

_DESTINATION_DESCRIPTION = (
    "Directory name the artifacts are written under, inside the run's own artifact "
    "directory. Defaults to `build/`."
)


def _builtin_stub(*args: object, **kwargs: object) -> str:
    """Never invoked; bound so validation resolves names the way execution does."""
    return ""


def validate_starlark_report_code(code: str, function: str | None) -> None:
    """Raise ValueError unless executing `code` binds `function` to a function."""
    wanted = function or DEFAULT_FUNCTION_NAME
    candidates = (wanted,) if wanted == DEFAULT_FUNCTION_NAME else (wanted, DEFAULT_FUNCTION_NAME)
    builtins = {name: _builtin_stub for name in (REFUSE_BUILTIN, *REPORT_BUILTINS)}
    try:
        module = compile_starlark_module(code, builtins)
        bound = find_bound_function(module, candidates)
    except starlark.StarlarkError as exc:
        raise ValueError(
            f"Starlark code does not compile: {exc}. {STARLARK_LANGUAGE_NOTE}"
        ) from exc
    except ValueError as exc:
        raise ValueError(f"field `function`: {exc}") from exc
    if bound is None:
        raise ValueError(
            f"Starlark code must define `def {wanted}(...)` at the top level — the runtime "
            f"calls it with one list of row dicts per declared input, and expects nothing "
            f"back: the artifact is what {EMIT_FILE_BUILTIN}/{EMIT_TABLE_BUILTIN} wrote"
        )


class StarlarkReport(StageConfig):
    FINGERPRINT_FIELDS: ClassVar[frozenset[str]] = frozenset({
        "code", "function", "destination",
    })
    INCIDENTAL_FIELDS: ClassVar[frozenset[str]] = frozenset({"summary", "corner_cases"})

    summary: Optional[str] = Field(default=None, description=SUMMARY_DESCRIPTION)
    corner_cases: list[CornerCase] = Field(
        default_factory=list, description=CORNER_CASES_DESCRIPTION
    )
    code: str = Field(description=_CODE_DESCRIPTION)
    function: Optional[str] = Field(default=None, description=_FUNCTION_DESCRIPTION)
    destination: Optional[str] = Field(default=None, description=_DESTINATION_DESCRIPTION)

    @model_validator(mode="after")
    def _source_is_runnable(block: "StarlarkReport") -> "StarlarkReport":
        validate_starlark_report_code(block.code, block.function)
        return block


class StarlarkReportStage(AbstractStage):
    # The sandboxed member of the pair that emits files rather than a table.
    REQUIRES_OUTPUT_SCHEMA: ClassVar[bool] = False

    type: Literal[StageType.starlark_report]
    CACHE_IGNORED_BECAUSE: ClassVar[str] = (
        "a report writes the artifacts a reader opens, and a replayed frame would skip the write"
    )
    starlark_report: StarlarkReport
    inputs: list[StageInput] = Field(default_factory=list, min_length=1)
    signature: ReplacesSignature

    def fingerprint_blocks(self) -> dict[str, StageConfig]:
        return {"starlark_report": self.starlark_report}

    def find_authored_code_block(self) -> StarlarkReport:
        return self.starlark_report

    def find_handle_compiler_warnings(self) -> list[CompilerWarning]:
        if (self.starlark_report.summary or "").strip():
            return []
        return [warn(self, "undescribed",
                     "no plain-language description — reviewable only by reading its code")]

    def find_signature_config_issues(self) -> list[str]:
        if self.signature.produces:
            return [
                f"stage '{self.id}': starlark_report emits files, not a table — "
                f"signature produces must be empty"
            ]
        return []

# Authoring copy for this module's stage type(s); assembled into STAGE_TYPES.
STAGE_TYPE_SPECS: dict[str, StageTypeSpec] = {
    "starlark_report": StageTypeSpec(
        summary="Sandboxed Starlark that writes the final artifact (html, csv, json).",
        signature_form="replaces",
        blocks=["starlark_report"],
        requires_inputs=True,
        min_inputs=1,
        required=["code"],
        optional=["function", "destination", "summary"],
        notes=(
            "A workflow need not have one — a run whose result is a table is finished "
            "without it.\n"
            "Reported output must be INTERROGABLE: every figure and every row it renders "
            "says where it came from, which is what the two citation builtins are for. "
            "Nothing reads the artifact back, so printing a number other than the one cited "
            "beside it is NOT caught — print the figure and its link together, in one cell "
            "or one line, so a reader meets the number and its source at once.\n"
            "The whole input is marshalled into the sandbox as lists of row dicts, so "
            "reshaping work — grouping, sorting, deduping, joining — belongs in the stages "
            "ahead of this one, where the runtime records what it did.\n"
            "The one signature form that produces nothing: it emits files, not a table.\n"
            + STARLARK_LANGUAGE_NOTE
        ),
    ),
}

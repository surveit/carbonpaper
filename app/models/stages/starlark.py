"""starlark_row_function stage: the config block, plus write-time validation that
its inline Starlark compiles and binds the wanted function. Sandboxed inline code
only — no `kind`/`module`, unlike PythonFunction, since there is no importable
Starlark module to point at."""
from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar, Literal, Optional

import starlark
from pydantic import Field, model_validator

from app.core.starlark_source import (
    DEFAULT_FUNCTION_NAME,
    REFUSE_BUILTIN,
    compile_starlark_module,
    find_bound_function,
)
from app.models.schema import (
    Column,
    DATE_COLUMN_TYPES,
    StageConfig,
    find_list_element_type,
)
from app.models.stages.stage_base import AbstractStage, StageInput, StageType
from app.models.stages.code import CORNER_CASES_DESCRIPTION, SUMMARY_DESCRIPTION, CornerCase
from app.models.stages.stage_type_spec import StageTypeSpec
from app.models.stages.signature import ExtendsSignature
from app.models.stages.stage_tests import StarlarkRowFunctionStageTest
from app.models.stages.warnings import CompilerWarning, warn

# REFUSE_BUILTIN is registered so write-time validation compiles source in the
# same shape execution does: Starlark resolves free variables STATICALLY at
# module load, so source whose body calls `refuse()` fails to load unless the
# name is already bound — even though this stub is never actually called.

# Stated ONCE and rendered into every Starlark surface: the two stage types'
# `code` descriptions and their authoring notes. `is` earns its place because it
# is the one limit that bites idiomatic Python rather than reaching outside the
# sandbox — 22 of the 24 filter predicates in the store compiled unchanged, and
# `stated is None` was one of the two that did not.
STARLARK_LANGUAGE_NOTE = (
    "Starlark is Python's syntax without imports, file or network access, classes, "
    "`while`, try/except, or `is` — compare with `==`, including against None. "
    "Recursion runs but is bounded by a call-stack limit, so it cannot loop forever."
)

_VALUE_MARSHALLING_NOTE = (
    "Values arrive as strings, numbers, booleans, None, lists and dicts; dates and "
    "timestamps as ISO-8601 strings and every missing value as None."
)

# Not on the filter: it keeps or drops a row, so it writes no column to declare.
_DATE_COLUMN_NOTE = (
    "For a date, that ISO-8601 string is also the most it can RETURN, so it may not write a "
    "`date`/`datetime` column: declare that column `str` and give its format in the "
    "column description."
)

_FUNCTION_DESCRIPTION = (
    "Name of the function to call within `code`, defaulting to `transform`. `code` "
    "says what is defined; this says which name in it to call — set it only when the "
    "function is not called `transform`."
)

# `key=value` would be Starlark keyword syntax, so it cannot name a column
# with a space in it.
_CARRY_THROUGH_NOTE = (
    "The returned dict IS the output row: a key you do not return is absent, so carry "
    "columns through with `return dict(row, **{\"column\": value})`."
)

_CODE_DESCRIPTION = (
    "Inline Starlark defining `function` (default `transform`): `def transform(row): "
    "...`, one row dict in, one row dict out. "
    + _CARRY_THROUGH_NOTE + " " + STARLARK_LANGUAGE_NOTE + " " + _VALUE_MARSHALLING_NOTE +
    " " + _DATE_COLUMN_NOTE +
    " Call `refuse(\"reason\")` to decline a row you cannot honestly process; call "
    "`fail(\"reason\")` only for a bug. Module-level variables are frozen after "
    "load — keep state in locals."
)


def _refuse_stub(reason: str) -> None:
    """Never invoked; registered so validation resolves names the way execution does."""
    return None


def validate_starlark_function_code(
    code: str, function: str | None, default_name: str = DEFAULT_FUNCTION_NAME,
    return_hint: str = "a row dict",
) -> None:
    """Raise ValueError unless executing `code` binds `function` to a function."""
    wanted = function or default_name
    candidates = (wanted,) if wanted == default_name else (wanted, default_name)
    try:
        module = compile_starlark_module(code, {REFUSE_BUILTIN: _refuse_stub})
        bound = find_bound_function(module, candidates)
    except starlark.StarlarkError as exc:
        raise ValueError(
            f"Starlark code does not compile: {exc}. Starlark has no import, "
            "while, recursion, try/except, or classes."
        ) from exc
    except ValueError as exc:
        raise ValueError(f"field `function`: {exc}") from exc
    if bound is None:
        raise ValueError(
            f"Starlark code must define `def {wanted}(row): ...` at the top level — "
            f"the runtime calls {wanted}(row) per row and expects {return_hint} back"
        )


class StarlarkFunction(StageConfig):
    FINGERPRINT_FIELDS: ClassVar[frozenset[str]] = frozenset({"code", "function"})
    INCIDENTAL_FIELDS: ClassVar[frozenset[str]] = frozenset({"summary", "corner_cases"})

    summary: Optional[str] = Field(default=None, description=SUMMARY_DESCRIPTION)
    corner_cases: list[CornerCase] = Field(
        default_factory=list, description=CORNER_CASES_DESCRIPTION
    )
    code: str = Field(description=_CODE_DESCRIPTION)
    function: Optional[str] = Field(default=None, description=_FUNCTION_DESCRIPTION)

    @model_validator(mode="after")
    def _source_is_runnable(block: "StarlarkFunction") -> "StarlarkFunction":
        validate_starlark_function_code(block.code, block.function)
        return block


class StarlarkRowFunctionStage(AbstractStage):
    type: Literal[StageType.starlark_row_function]
    CARRIES_RUNNABLE_TESTS: ClassVar[bool] = True
    starlark: StarlarkFunction
    # Exactly one input: the runtime maps the function over one frame's rows.
    inputs: list[StageInput] = Field(default_factory=list, min_length=1, max_length=1)
    tests: Optional[Sequence[StarlarkRowFunctionStageTest]] = None
    # The code is opaque to load-time validation, so unlike the config-driven
    # types nothing here cross-checks the block. The function is held to its
    # claimed writes at run time instead: the stage's output frame is validated
    # against the output schema this signature resolves to.
    signature: ExtendsSignature

    def fingerprint_blocks(self) -> dict[str, StageConfig]:
        return {"starlark": self.starlark}

    def find_authored_code_block(self) -> StarlarkFunction:
        return self.starlark

    def find_handle_compiler_warnings(self) -> list[CompilerWarning]:
        return find_starlark_warnings(self)

    def find_signature_config_issues(self) -> list[str]:
        return find_starlark_signature_issues(self)


def find_starlark_signature_issues(stage: "StarlarkRowFunctionStage") -> list[str]:
    signature = stage.signature
    dates = _find_date_columns([*signature.adds, *signature.rewrites])
    if not dates:
        return []
    named = ", ".join(repr(name) for name in dates)
    return [
        f"stage '{stage.id}': starlark_row_function cannot write {named} — the declared "
        f"type holds a date, and Starlark has none: the function returns the ISO-8601 "
        f"string that spells one, and the row check refuses it. Declare the column `str`, "
        f"with its format in the description."
    ]


def _find_date_columns(columns: list[Column]) -> list[str]:
    return [column.name for column in columns if _is_date_column(column)]


def _is_date_column(column: Column) -> bool:
    return (
        _is_date_type(column.type)
        or _is_date_type(column.value_type or "")
        or any(_is_date_column(field) for field in column.fields or ())
    )


def _is_date_type(type_name: str) -> bool:
    element = find_list_element_type(type_name)
    return _is_date_type(element) if element else type_name in DATE_COLUMN_TYPES


def find_starlark_warnings(stage: "StarlarkRowFunctionStage") -> list[CompilerWarning]:
    if not (stage.starlark.summary or "").strip():
        return [warn(stage, "undescribed",
                     "no plain-language description — reviewable only by reading its code")]
    return []

# Authoring notes for this module's stage type(s), as the plain-data shape the
# authoring prompts render. Assembled into STAGE_TYPES by app.models.stages.
STAGE_TYPE_SPECS: dict[str, StageTypeSpec] = {
    "starlark_row_function": StageTypeSpec(
        summary="Sandboxed Starlark run once per row: one row in → one row out.",
        signature_form="extends",
        blocks=["starlark"],
        requires_inputs=True,
        min_inputs=1,
        required=["code"],
        optional=["function", "summary"],
        notes=(
            STARLARK_LANGUAGE_NOTE +
            " The step cannot read or write anything outside its row. "
            "`transform(row)` is handed a plain dict and must return a plain dict. "
            + _CARRY_THROUGH_NOTE + " "
            + _VALUE_MARSHALLING_NOTE + " " + _DATE_COLUMN_NOTE + " An integer "
            "beyond 2**63-1 stops the step rather than losing precision. Call "
            "`refuse(\"reason\")` to decline a row you cannot honestly process. "
            "Module-level variables freeze after load — keep state in locals."
        ),
    ),
}

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
from app.models.schema import StageConfig
from app.models.stages.stage_base import StageBase, StageInput, StageType
from app.models.stages.code import CORNER_CASES_DESCRIPTION, SUMMARY_DESCRIPTION, CornerCase
from app.models.stages.code import (
    CODE_CORNER_CASES_CONTRACT_NOTE,
    CODE_SUMMARY_CONTRACT_NOTE,
)
from app.models.stages.node_spec import NodeTypeSpec
from app.models.stages.signature import ExtendsSignature
from app.models.stages.stage_tests import StarlarkRowFunctionStageTest
from app.models.stages.warnings import CompilerWarning, warn

# REFUSE_BUILTIN is registered so write-time validation compiles source in the
# same shape execution does: Starlark resolves free variables STATICALLY at
# module load, so source whose body calls `refuse()` fails to load unless the
# name is already bound — even though this stub is never actually called.

_FUNCTION_DESCRIPTION = (
    "Name of the function to call within `code`, defaulting to `transform`. `code` "
    "says what is defined; this says which name in it to call — set it only when the "
    "function is not called `transform`."
)

_CODE_DESCRIPTION = (
    "Inline Starlark defining `function` (default `transform`): `def transform(row): "
    "...`, one row dict in, one row dict out, and the returned dict IS the output row "
    "(a key you do not return is absent — carry columns through with `return "
    "dict(row, key=value)`). Starlark is Python's syntax without imports, file or network "
    "access, classes, `while`, or try/except. Recursion is not rejected — a "
    "self-terminating recursive function runs — but is bounded by a call-stack limit "
    "(`Starlark call stack overflow`), so it cannot loop forever the way an unbounded "
    "`while` would. Row values arrive as "
    "strings, numbers, booleans, None, lists and dicts; dates and timestamps arrive "
    "as ISO-8601 strings and every missing value arrives as None. Call "
    "`refuse(\"reason\")` to decline a row you cannot honestly process; call "
    "`fail(\"reason\")` only for a bug. Module-level variables are frozen after "
    "load — keep state in locals."
)


def _refuse_stub(reason: str) -> None:
    """Never invoked; registered so validation resolves names the way execution does."""
    return None


def validate_starlark_function_code(
    code: str, function: str | None, default_name: str = DEFAULT_FUNCTION_NAME
) -> None:
    """Raise ValueError unless executing `code` binds `function` to a function."""
    wanted = function or default_name
    candidates = (
        (wanted,) if wanted == default_name
        else (wanted, default_name)
    )
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
            f"the runtime calls {wanted}(row) per row"
        )


class StarlarkFunction(StageConfig):
    """Config block for starlark_row_function: inline Starlark, no importable module."""
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
        # `block`, not `self`: a config-block field, not code.py's stage handle.
        validate_starlark_function_code(block.code, block.function)
        return block


class StarlarkRowFunctionStage(StageBase):
    type: Literal[StageType.starlark_row_function]
    CARRIES_RUNNABLE_TESTS: ClassVar[bool] = True
    starlark: StarlarkFunction
    # Exactly one input: the runtime maps the function over one frame's rows.
    inputs: list[StageInput] = Field(default_factory=list, min_length=1, max_length=1)
    tests: Optional[Sequence[StarlarkRowFunctionStageTest]] = None
    # The code is opaque to load-time validation, so unlike the config-driven
    # types nothing here cross-checks the block. The function is held to its
    # claimed writes at run time instead: the stage's output frame is validated
    # against output_schema, which find_signature_issues pins to this
    # signature.
    signature: ExtendsSignature

    def fingerprint_blocks(self) -> dict[str, StageConfig]:
        return {"starlark": self.starlark}

    def find_authored_code_block(self) -> StarlarkFunction:
        return self.starlark

    def find_handle_compiler_warnings(self) -> list[CompilerWarning]:
        return find_starlark_warnings(self)


def find_starlark_warnings(stage: "StarlarkRowFunctionStage") -> list[CompilerWarning]:
    """Warnings about `stage.starlark` — raised here and only here, since this
    module owns it."""
    if not (stage.starlark.summary or "").strip():
        return [warn(stage, "undescribed",
                     "no plain-language description — reviewable only by reading its code")]
    return []

# Authoring notes for this module's stage type(s), as the plain-data shape the
# authoring prompts render. Assembled into NODE_TYPES by app.models.stages.
NODE_TYPE_SPECS: dict[str, NodeTypeSpec] = {
    "starlark_row_function": NodeTypeSpec(
        summary="Sandboxed Starlark run once per row: one row in → one row out. Prefer this over python_row_function.",
        signature_form="extends",
        blocks=["starlark"],
        requires_inputs=True,
        min_inputs=1,
        required=["code"],
        optional=["function", "summary"],
        notes=(
            "PREFER THIS over python_row_function for row transforms. Starlark is Python's "
            "syntax minus imports, file and network access, classes, while, and try/except, "
            "so the step cannot read or write anything outside its row. Recursion is not "
            "rejected — a self-terminating recursive function runs — but is bounded by a "
            "call-stack limit (a `Starlark call stack overflow`), so it cannot loop forever "
            "the way an unbounded while would. "
            "`transform(row)` is handed a plain dict and must return a plain dict, and that "
            "dict IS the output row: a key you do not return is absent, so carry columns "
            "through explicitly (`return dict(row, key=value)`). Values arrive as strings, numbers, "
            "booleans, None, lists and dicts; dates and timestamps arrive as ISO-8601 "
            "strings and every missing value arrives as None. An integer beyond 2**63-1 "
            "stops the step rather than losing precision. Call `refuse(\"reason\")` to "
            "decline a row you cannot honestly process. Module-level variables freeze after "
            "load — keep state in locals. Use python_row_function only when the step "
            "genuinely needs a Python library."
            f" {CODE_SUMMARY_CONTRACT_NOTE} {CODE_CORNER_CASES_CONTRACT_NOTE}"
        ),
    ),
}

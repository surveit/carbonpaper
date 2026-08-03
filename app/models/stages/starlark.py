"""starlark_row_function stage: the config block, plus write-time validation that
its inline Starlark compiles and binds the wanted function. Sandboxed inline code
only — no `kind`/`module`, unlike PythonFunction, since there is no importable
Starlark module to point at."""
from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar, Literal, Optional

import starlark
from pydantic import Field, model_validator

from app.core.starlark_source import compile_starlark_module, find_bound_function
from app.models.schema import StageConfig
from app.models.stage_base import StageBase, StageInput, StageType
from app.models.stages.code import CORNER_CASES_DESCRIPTION, SUMMARY_DESCRIPTION, CornerCase
from app.models.stages.stage_tests import StarlarkRowFunctionStageTest
from app.models.stages.warnings import CompilerWarning, warn

# The name `validate_starlark_function_code` calls when it does not find `function`
# bound — the runtime's own default (app.runtime.starlark_code registers the same
# fallback name independently, since the model layer may not import the runtime).
_DEFAULT_FUNCTION_NAME = "transform"

# The builtin registered so write-time validation compiles source in the same
# shape execution does: Starlark resolves free variables STATICALLY at module
# load, so source whose body calls `refuse()` fails to load unless the name is
# already bound — even though this stub is never actually called.
_REFUSE_BUILTIN = "refuse"

_FUNCTION_DESCRIPTION = (
    "Name of the function to call within `code`, defaulting to `transform`. `code` "
    "says what is defined; this says which name in it to call — set it only when the "
    "function is not called `transform`."
)

_CODE_DESCRIPTION = (
    "Inline Starlark defining `function` (default `transform`): `def transform(row): "
    "...`, one row dict in, one row dict out, and the returned dict IS the output row "
    "(a key you do not return is absent — carry columns through with `return "
    "{**row, ...}`). Starlark is Python's syntax without imports, file or network "
    "access, classes, `while`, recursion, or try/except. Row values arrive as "
    "strings, numbers, booleans, None, lists and dicts; dates and timestamps arrive "
    "as ISO-8601 strings and every missing value arrives as None. Call "
    "`refuse(\"reason\")` to decline a row you cannot honestly process; call "
    "`fail(\"reason\")` only for a bug. Module-level variables are frozen after "
    "load — keep state in locals."
)


def _refuse_stub(reason: str) -> None:
    """Never invoked; registered so validation resolves names the way execution does."""
    return None


def validate_starlark_function_code(code: str, function: str | None) -> None:
    """Raise ValueError unless `code` binds `function` (or `transform`) to a function."""
    wanted = function or _DEFAULT_FUNCTION_NAME
    candidates = (
        (wanted,) if wanted == _DEFAULT_FUNCTION_NAME
        else (wanted, _DEFAULT_FUNCTION_NAME)
    )
    try:
        module = compile_starlark_module(code, {_REFUSE_BUILTIN: _refuse_stub})
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
        # Named `block`, not `self`: this is a config block's own field, not the
        # stage-level `function` handle app/models/stages/code.py owns (see
        # tests/arch/test_handle_access_is_owned.py).
        validate_starlark_function_code(block.code, block.function)
        return block


class StarlarkRowFunctionStage(StageBase):
    type: Literal[StageType.starlark_row_function]
    CARRIES_RUNNABLE_TESTS: ClassVar[bool] = True
    starlark: StarlarkFunction
    # Exactly one input: the runtime maps the function over one frame's rows.
    inputs: list[StageInput] = Field(default_factory=list, min_length=1, max_length=1)
    tests: Optional[Sequence[StarlarkRowFunctionStageTest]] = None

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

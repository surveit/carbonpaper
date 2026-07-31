"""The python-code block and its validation: the inline code a python_row_function /
python_frame_function (or a publish stage's function block) carries must parse,
compile, and define the function the runtime calls. Also holds the wording of the
`summary` every authored-code block asks for, so PythonFunction and FilterConfig
cannot drift apart."""
from __future__ import annotations

import ast

from typing import ClassVar, Literal, Optional, Protocol

from pydantic import Field, model_validator

from app.models.errors import StepRefused
from app.models.schema import FunctionKind, StageConfig, _Base
from app.models.stage_base import StageBase, StageInput, StageType
from app.models.stages.warnings import CompilerWarning, warn

# The instruction an authoring client reads when it fills in `summary`. Python
# code is the one block a non-engineer reviewer cannot read for themselves, so
# the summary — not the code — is what the stage page leads with; it is written
# alongside the code, from the methodology, and says the RULE rather than the
# implementation.
SUMMARY_DESCRIPTION = (
    "REQUIRED in practice: one or two plain sentences telling a non-engineer what this "
    "step does, written from the methodology at the same time as the code. State the "
    "rule and its intent, not the implementation — \"marks a bill withdrawn when its "
    "status text says so, leaving the score blank\", never \"applies a regex to `status` "
    "and returns a dict\". No Python vocabulary (function, dict, DataFrame, None, "
    "regex); the only identifiers to use are column names the reader already sees in "
    "the schema."
)

# The instruction for `corner_cases`. Split from `summary` on purpose: the summary
# has to stay short enough for a non-engineer to actually read, which means edge
# cases either bloat it or go unsaid — and unsaid is how a description ends up
# TRUE but incomplete, agreeing with the code on the common path while saying
# nothing about the input that will actually bite. Both fields are handed to the
# example deriver, so anything named here becomes a case that must pass.
CORNER_CASES_DESCRIPTION = (
    "The inputs where this step's behaviour is not obvious from the summary, each paired "
    "with what must happen. Write one entry per case, from the methodology, at the same "
    "time as the code — blank or missing values, values that cannot be parsed, "
    "boundaries and thresholds (state which side is inclusive), ties, duplicates, empty "
    "input, values outside an expected set. `expected` states the OUTCOME in the same "
    "plain language as the summary (\"the row is left unchanged\", \"the step fails\", "
    "\"treated as zero\"), never the implementation. If a case is genuinely undecided by "
    "the methodology, say so in `expected` and name the reading you chose. These are "
    "handed to the agent that derives this step's examples, so each entry becomes a case "
    "the code must satisfy: do not list a case whose outcome you are inventing."
)


class CornerCase(_Base):
    """One input where a step's behaviour needs stating, and what must happen."""

    case: str = Field(
        description=(
            "The input, in plain language — \"the reported amount is blank\", \"two "
            "filings report the same amount\". Name columns the reader already sees in "
            "the schema, in `backticks`."
        ),
    )
    expected: str = Field(
        description=(
            "What must happen for that input, as an outcome a non-engineer can check — "
            "\"the row is kept with the amount treated as zero\", \"the step fails\"."
        ),
    )


class AuthoredCode(Protocol):
    """What every authored-code block carries for a reviewer: the plain-language
    account of the code, and the cases it must handle. Structural, so a config
    class satisfies it by declaring the two fields —
    `StageBase.find_authored_code_block` returns whichever block a stage has."""

    summary: Optional[str]
    corner_cases: list[CornerCase]


def _binds_name(tree: ast.Module, name: str) -> bool:
    """True if a top-level def or assignment in `tree` binds `name` — i.e. what
    `exec`ing the code would expose for the runtime to look up and call."""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return True
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return True
    return False


def validate_inline_function_code(
    code: str,
    function: str | None,
    default_name: str = "transform",
    return_hint: str = "a dict",
) -> None:
    """Raise ValueError if inline function `code` does not compile or does not
    define the function the runtime calls (`default_name` unless `function`
    names another).

    A single stage's invariant, enforced at write time so broken code (e.g. a
    bare body with a top-level `return`) is rejected before the runner exec()s it."""
    try:
        tree = ast.parse(code)
        # compile() (like the runtime's exec) also catches a top-level `return`,
        # which ast.parse alone does not on 3.12.
        compile(code, "<inline function>", "exec")
    except SyntaxError as exc:
        raise ValueError(
            f"inline function code does not compile: {exc.msg} (line {exc.lineno}). "
            f"Define a function, e.g. `def {default_name}(row): ...; return row`."
        )
    wanted = function or default_name
    if not (_binds_name(tree, wanted) or _binds_name(tree, default_name)):
        raise ValueError(
            f"inline function code must define `def {wanted}(...)` at the top level — "
            f"the runtime calls {wanted}(row) per row and expects {return_hint} back"
        )


class PythonFunction(StageConfig):
    """Config block for python_row_function / python_frame_function (and publish). The
    row-vs-frame distinction lives in the stage `type`, not here — the runtime
    reads the type to decide whether to invoke this per row or per frame."""
    # Every field changes what this stage computes (the code/module it runs)
    # except `summary`, which describes that code to a reader — see
    # StageBase.compute_definition_fingerprint.
    FINGERPRINT_FIELDS: ClassVar[frozenset[str]] = frozenset({
        "kind", "code", "module", "function", "requirements",
    })
    INCIDENTAL_FIELDS: ClassVar[frozenset[str]] = frozenset({"summary", "corner_cases"})

    kind: FunctionKind
    summary: Optional[str] = Field(default=None, description=SUMMARY_DESCRIPTION)
    corner_cases: list[CornerCase] = Field(
        default_factory=list, description=CORNER_CASES_DESCRIPTION
    )
    code: Optional[str] = Field(
        default=None,
        description=(
            "Inline Python defining `function` (default `transform`). Signature by stage "
            "type: python_row_function `def transform(row: dict) -> dict` (1 row in, 1 out; "
            "cannot reorder or fan out); python_frame_function "
            "`def transform(df, ...) -> DataFrame` (inputs positional in declared order); "
            "publish `def transform(df, ..., output_dir, trace_links) -> DataFrame` (writes "
            "artifact files into output_dir; the returned frame lists them). When the "
            "function meets an input it cannot handle, it refuses instead of "
            f"returning: `raise {StepRefused.__name__}(\"...\")`, which needs no import — the name is "
            "already in scope. The message names the input and says, in language a "
            "non-engineer can read, why that input cannot be handled. "
            "This helps narrow inputs further, for example if price expects a string like '$45,000.00' "
            "then it should throw an error if it sees '€45.000,00' for example if it doesn't "
            "have a forex conversion table and it will break sums downstream."
        ),
    )
    module: Optional[str] = None
    function: Optional[str] = None
    requirements: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _kind_fields(self) -> "PythonFunction":
        if self.kind == FunctionKind.module and not self.module:
            raise ValueError("function.kind=module needs `module`")
        if self.kind == FunctionKind.inline and not self.code:
            raise ValueError("function.kind=inline needs `code`")
        return self

    @model_validator(mode="after")
    def _inline_code_is_runnable(self) -> "PythonFunction":
        """Inline code must parse and define the function the runtime calls
        (`transform` by default). Enforced here — a single stage's invariant — so
        broken code (e.g. a bare body with a top-level `return`) is rejected at
        write time instead of raising only when the runner exec()s it."""
        if self.kind != FunctionKind.inline or not self.code:
            return self
        validate_inline_function_code(self.code, self.function)
        return self


def find_python_function_warnings(stage: "CarriesPythonFunctionStage"
                                  ) -> list[CompilerWarning]:
    """Warnings about a `function` block — raised here and only here, since this module owns
    it."""
    function = stage.function
    if not (function.summary or "").strip():
        return [warn(stage, "undescribed",
                     "no plain-language description — reviewable only by reading its code")]
    if function.kind == FunctionKind.module:
        return [warn(stage, "unreviewable_code",
                     f"the code lives in module `{function.module}` rather than on the "
                     f"stage, so the review panel cannot show it")]
    return []


class CarriesPythonFunctionStage(StageBase):
    """Stage types whose behaviour is a `function` block; declared once so `.function` is read
    here."""
    function: PythonFunction

    def fingerprint_blocks(self) -> dict[str, StageConfig]:
        return {"function": self.function}

    def find_authored_code_block(self) -> PythonFunction:
        return self.function

    def find_handle_compiler_warnings(self) -> list[CompilerWarning]:
        return find_python_function_warnings(self)


class PythonRowFunctionStage(CarriesPythonFunctionStage):
    type: Literal[StageType.python_row_function]
    # Exactly one input: the runtime maps the function over one frame's rows, so
    # a second input is a join or a python_frame_function.
    inputs: list[StageInput] = Field(default_factory=list, min_length=1, max_length=1)


class PythonFrameFunctionStage(CarriesPythonFunctionStage):
    type: Literal[StageType.python_frame_function]
    inputs: list[StageInput] = Field(default_factory=list, min_length=1)

"""Authored code must parse, compile and define the function the runtime calls."""
from __future__ import annotations

import ast

from collections.abc import Sequence
from typing import ClassVar, Literal, Optional, Protocol

from pydantic import ConfigDict, Field, model_validator

from app.models.errors import StepRefused
from app.models.schema import FunctionKind, StageConfig, _Base
from app.models.stages.stage_base import AbstractStage, StageInput, StageType
from app.models.stages.stage_type_spec import StageTypeSpec
from app.models.stages.signature import ExtendsSignature, ReplacesSignature
from app.models.stages.stage_tests import (
    PythonFrameFunctionStageTest,
    PythonRowFunctionStageTest,
)
from app.models.stages.warnings import CompilerWarning, warn
from app.models.tool_schema_prompts import (
    CORNER_CASE_DESCRIPTION,
    PYTHON_FUNCTION_DESCRIPTION,
)

# The instruction an authoring client reads when it fills in `summary`. Python
# code is the one block a non-engineer reviewer cannot read for themselves, so
# the summary — not the code — is what the stage page leads with; it is written
# alongside the code, from the methodology, and says the RULE rather than the
# implementation.
# A summary a non-engineer will actually read is short. Enforced on WRITE
# (services.stage_edit), not on the model: stages stored before the limit — and every
# frozen version — must still load.
SUMMARY_MAX_CHARS = 255

SUMMARY_DESCRIPTION = (
    "REQUIRED in practice: one or two plain sentences telling a non-engineer what this "
    "step does, written from the methodology at the same time as the code. State the "
    "rule and its intent, not the implementation — \"marks a bill withdrawn when its "
    "status text says so, leaving the score blank\", never \"applies a regex to `status` "
    "and returns a dict\". No Python vocabulary (function, dict, DataFrame, None, "
    "regex); the only identifiers to use are column names the reader already sees in "
    f"the schema. HARD LIMIT: {SUMMARY_MAX_CHARS} characters, refused above that — say "
    "the rule and stop. Anything conditional or surprising belongs in `corner_cases`, "
    "not here."
)

# The instruction for `corner_cases`. Split from `summary` on purpose: the summary
# has to stay short enough for a non-engineer to actually read, which means edge
# cases either bloat it or go unsaid — and unsaid is how a description ends up
# TRUE but incomplete, agreeing with the code on the common path while saying
# nothing about the input that will actually bite. Both fields are handed to the
# test generator, so anything named here becomes a case that must pass.
CORNER_CASES_DESCRIPTION = (
    "The inputs where this step's behaviour is not obvious from the summary, each paired "
    "with what must happen. Write one entry per case, from the methodology, at the same "
    "time as the code — blank or missing values, values that cannot be parsed, "
    "boundaries and thresholds (state which side is inclusive), ties, duplicates, empty "
    "input, values outside an expected set. `expected` states the OUTCOME in the same "
    "plain language as the summary (\"the row is left unchanged\", \"the step fails\", "
    "\"treated as zero\"), never the implementation. If a case is genuinely undecided by "
    "the methodology, say so in `expected` and name the reading you chose. These are "
    "handed to the agent that generates this step's examples, so each entry becomes a case "
    "the code must satisfy: do not list a case whose outcome you are inventing."
)


# Appended to the notes of every type carrying authored code. The limit is
# interpolated so the prompt cannot outlive the number stage_edit refuses on.
CODE_SUMMARY_CONTRACT_NOTE = (
    "The description is a BUDGET ON THE CODE: the block's `summary` "
    f"({SUMMARY_MAX_CHARS} characters, refused above that) plus its `corner_cases` is "
    "the whole space this step's behaviour gets. Author both in the same edit as the "
    "code.\n"
    "An independent agent will generate tests seeing only the description and corner "
    "cases, and must reproduce the behaviour of the code from that short description.\n"
    "If the behaviour will not fit precisely enough to reconstruct it, the step does "
    "too much — downscope it or split the stage. The description cannot grow to fit "
    "the code."
)

# One sentence on purpose: the field's own description carries the substance.
CODE_CORNER_CASES_CONTRACT_NOTE = (
    "ALWAYS also submit the block's `corner_cases` alongside the summary, in the same "
    "edit — an empty list if the step genuinely has none, but never omitted."
)


class CornerCase(_Base):
    model_config = ConfigDict(json_schema_extra={"description": CORNER_CASE_DESCRIPTION})

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
    summary: Optional[str]
    corner_cases: list[CornerCase]
    code: str
    function: Optional[str]


def _binds_name(tree: ast.Module, name: str) -> bool:
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
    model_config = ConfigDict(json_schema_extra={"description": PYTHON_FUNCTION_DESCRIPTION})

    FINGERPRINT_FIELDS: ClassVar[frozenset[str]] = frozenset({
        "kind", "code", "function", "requirements",
    })
    INCIDENTAL_FIELDS: ClassVar[frozenset[str]] = frozenset({"summary", "corner_cases"})

    kind: FunctionKind
    summary: Optional[str] = Field(default=None, description=SUMMARY_DESCRIPTION)
    corner_cases: list[CornerCase] = Field(
        default_factory=list, description=CORNER_CASES_DESCRIPTION
    )
    code: str = Field(
        description=(
            "Inline Python defining `function` (default `transform`). Signature by stage "
            "type: python_row_function `def transform(row: dict) -> dict` (1 row in, 1 out; "
            "cannot reorder or fan out); python_frame_function "
            "`def transform(df, ..., *, progress) -> DataFrame` (inputs positional in "
            "declared order; it may declare the keyword-only progress callback, which "
            "accepts completed and total); "
            "report `def transform(df, ..., output_dir, citation_provider) -> DataFrame` (writes "
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
    function: Optional[str] = None
    requirements: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _inline_code_is_runnable(self) -> "PythonFunction":
        validate_inline_function_code(self.code, self.function)
        return self


def find_python_function_warnings(stage: "CarriesPythonFunctionStage"
                                  ) -> list[CompilerWarning]:
    function = stage.function
    if not (function.summary or "").strip():
        return [warn(stage, "undescribed",
                     "no plain-language description — reviewable only by reading its code")]
    return []


class CarriesPythonFunctionStage(AbstractStage):
    function: PythonFunction

    def fingerprint_blocks(self) -> dict[str, StageConfig]:
        return {"function": self.function}

    def find_authored_code_block(self) -> PythonFunction:
        return self.function

    def find_handle_compiler_warnings(self) -> list[CompilerWarning]:
        return find_python_function_warnings(self)


class PythonRowFunctionStage(CarriesPythonFunctionStage):
    type: Literal[StageType.python_row_function]
    CARRIES_RUNNABLE_TESTS: ClassVar[bool] = True
    # Exactly one input: the runtime maps the function over one frame's rows, so
    # a second input is a join or a python_frame_function.
    inputs: list[StageInput] = Field(default_factory=list, min_length=1, max_length=1)
    tests: Optional[Sequence[PythonRowFunctionStageTest]] = None
    # The code is opaque to load-time validation, so unlike the config-driven
    # types nothing here cross-checks the block. The function is held to its
    # claimed writes at run time instead: the stage's output frame is validated
    # against the output schema this signature resolves to.
    signature: ExtendsSignature


class PythonFrameFunctionStage(CarriesPythonFunctionStage):
    type: Literal[StageType.python_frame_function]
    CACHE_IGNORED_BECAUSE: ClassVar[str] = (
        "hashing the whole input frame costs more than re-running the transform on it"
    )
    CARRIES_RUNNABLE_TESTS: ClassVar[bool] = True
    inputs: list[StageInput] = Field(default_factory=list, min_length=1)
    tests: Optional[Sequence[PythonFrameFunctionStageTest]] = None
    signature: ReplacesSignature

# Authoring copy for this module's stage type(s); assembled into STAGE_TYPES.
STAGE_TYPE_SPECS: dict[str, StageTypeSpec] = {
    "python_row_function": StageTypeSpec(
        summary="Python run once per row: one row in → one row out (cannot fan rows out/in or reorder).",
        signature_form="extends",
        blocks=["function"],
        requires_inputs=True,
        min_inputs=1,
        required=["kind", "code"],
        optional=["function", "requirements", "summary"],
        notes=(
            "Prefer starlark_row_function, which runs sandboxed with no file, network, or "
            "library access — reach for this Python variant only when the transform genuinely "
            "needs a Python library (e.g. regex, date parsing beyond ISO-8601, numpy math) "
            "that Starlark's builtin-only environment cannot express. "
            "Takes exactly ONE input — to combine data from another input use enrich/expand, "
            "or python_frame_function. "
            "`transform(row)` is handed a plain dict and must return a plain dict, and that "
            "dict IS the output row: a key you do not return is absent from the output, so "
            "carry columns through explicitly (`return {**row, ...}`). The function is shown "
            "neither the frame nor the row's position, so it cannot fan out, drop or reorder."
        ),
    ),
    "python_frame_function": StageTypeSpec(
        summary="Python over the whole dataframe(s); may reshape (dedup, pivot, multi-input merge).",
        signature_form="replaces",
        blocks=["function"],
        requires_inputs=True,
        min_inputs=1,
        required=["kind", "code"],
        optional=["function", "requirements", "summary"],
        notes=(
            "The runtime calls `transform(*frames)`: one POSITIONAL parameter per declared "
            "input, in `inputs` order — never by name, never a dict of frames. It receives no "
            "output_dir and no citation_provider; writing files is the report's job. Return the output "
            "DataFrame."
        ),
    ),
}

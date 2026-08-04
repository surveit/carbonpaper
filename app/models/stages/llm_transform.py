"""llm_transform stage: the config block, the prompt-wiring checks (every
`{placeholder}` resolves against the input edge, and no input column is
double-braced), and the 1:1 additive contract its declared schemas must meet."""
from __future__ import annotations

import re
from typing import Any, ClassVar, Literal, Optional

from pydantic import AliasChoices, Field, model_validator

from app.core.llm.options import LLMModel
from app.core.prompt_template import find_template_fields
from app.models.schema import StageConfig, TableSchema
from app.models.stages.stage_base import StageBase, StageInput, StageType
from app.models.stages.shared import COLUMN_ISSUE, resolve_input_columns
from app.models.stages.node_spec import NodeTypeSpec
from app.models.stages.signature import ExtendsSignature


# Tool names an `llm_transform` stage may be granted, so a stage can RESEARCH — look
# a claim up on the open web, read the document behind it, extract text from a PDF —
# instead of answering only from the row in front of it.
#
# This set exists to catch TYPOS, not to police capability: `websearch` silently
# granting nothing is a bad failure, so an unknown name is refused loudly. What a
# stage should be trusted with is the pipeline author's call, not this module's.
#
# Granting any of these has a real consequence worth stating once: the stage's output
# stops being a pure function of its input row, so re-running it need not reproduce
# the same answer, and the stage cache cannot be relied on to stand in for a re-run.
# `Bash` in particular buys document extraction (pdftotext and friends) at the price
# of a stage that can run arbitrary commands. That trade is the author's to make.
#
# `Write` and `Edit` are not here yet — not as a judgement, just as an unmade
# decision. Nothing about the plumbing stops them being added.
GRANTABLE_TOOLS: frozenset[str] = frozenset({
    "WebSearch", "WebFetch", "Bash", "Read", "Grep", "Glob",
})


class LLMConfig(StageConfig):
    """llm_transform config block."""
    # Every field changes what this stage computes (the prompt, the model, the
    # sampling/response knobs) — see StageBase.compute_definition_fingerprint.
    FINGERPRINT_FIELDS: ClassVar[frozenset[str]] = frozenset({
        "prompt_instructions", "prompt_data_template", "model", "temperature",
        "max_retries", "response_format", "rubric", "tools", "batch_size",
    })
    INCIDENTAL_FIELDS: ClassVar[frozenset[str]] = frozenset()

    prompt_instructions: str = ""
    prompt_data_template: str = Field(
        validation_alias=AliasChoices("prompt_data_template", "prompt_template"),
        description=(
            "Sent to the model once per input row, rendered with Python's "
            "str.format_map over the row — inject a column as {column_name}. "
            "Row-invariant guidance belongs in prompt_instructions."
        ),
    )
    model: Optional[LLMModel] = None
    temperature: float = 0.0
    max_retries: int = 3
    response_format: Literal["json", "text"] = "json"
    rubric: Optional[dict[str, Any]] = None
    tools: Optional[list[str]] = Field(
        default=None,
        description=(
            "Tools this stage may use, from GRANTABLE_TOOLS. Omit for a stage that "
            "should answer only from its input row. A stage with tools is slower, "
            "costs materially more per row, and its output is NOT reproducible from "
            "the row alone."
        ),
    )
    batch_size: int = Field(
        default=1,
        ge=1,
        description=(
            "Rows per model call (default 1). >1 amortizes the prompt_instructions "
            "prefix across rows, which matters when that prefix is large; the runtime "
            "still returns exactly one row out per row in. But batch-mates share one "
            "context, so a row's answer can be influenced by them — keep 1 when each "
            "row needs an independent judgment."
        ),
    )

    @model_validator(mode="after")
    def _tools_are_known_names(self) -> "LLMConfig":
        if not self.tools:
            return self
        unknown = sorted(set(self.tools) - GRANTABLE_TOOLS)
        if unknown:
            # Typo protection: a misspelled name would otherwise grant nothing and
            # leave the stage quietly unable to do the work it was authored to do.
            raise ValueError(
                f"llm.tools: unknown tool name(s) {unknown}; known names are "
                f"{sorted(GRANTABLE_TOOLS)} (names are case-sensitive)."
            )
        if self.batch_size > 1:
            # Batch-mates share one context, so one row's research would contaminate
            # the next one's answer. Research rows must be judged independently.
            raise ValueError(
                "llm.tools requires batch_size=1: batched rows share a context, so "
                "one row's findings would leak into another's answer."
            )
        return self


class LLMTransformStage(StageBase):
    type: Literal[StageType.llm_transform]
    llm: LLMConfig
    inputs: list[StageInput] = Field(default_factory=list, min_length=1)
    signature: Optional[ExtendsSignature] = None

    def fingerprint_blocks(self) -> dict[str, StageConfig]:
        return {"llm": self.llm}

    def find_config_column_issues(self) -> list[str]:
        return find_llm_prompt_column_issues(self)

    def find_signature_config_issues(self) -> list[str]:
        return find_llm_signature_issues(self)

    def llm_reply_schema(self) -> Optional[TableSchema]:
        """What the model's reply itself must carry: `output_schema` minus the
        input schema — the columns this stage ADDS, since an llm_transform
        passes its input columns through untouched and the runtime rejoins them
        itself. This is the single definition of that spec: the runtime compiles
        the reply model from it (app.runtime.stages.llm_transform) and the stage
        panel displays it, so neither can drift from the other.

        None unless both schemas are declared. When they are,
        `_llm_transform_one_to_one` has already guaranteed the difference is
        well defined, so `subtract` cannot throw."""
        if not self.inputs:
            return None
        input_schema = self.inputs[0].table_schema
        output_schema = self.resolve_output_schema()
        if output_schema is None or input_schema is None:
            return None
        return output_schema.subtract(input_schema)

    @model_validator(mode="after")
    def _one_to_one(self) -> "LLMTransformStage":
        """Enforced here — a stage carries its own contract — so the reply spec
        the runtime computes (`output_schema.subtract(input_schema)`) is exactly
        the added columns and can never throw mid-run. This is about schema
        SHAPE, not config columns, so it is not part of
        find_config_column_issues."""
        issues = find_llm_one_to_one_issues(self)
        if issues:
            raise ValueError("llm_transform not strictly 1:1: " + "; ".join(issues))
        return self


def find_llm_signature_issues(stage: "LLMTransformStage") -> list[str]:
    """Reads must match the template's placeholders exactly; an llm_transform never rewrites."""
    signature = stage.signature
    assert signature is not None  # find_signature_config_issues runs only with one
    anchor_id = stage.inputs[0].id
    declared = {
        column.name
        for entry in signature.reads
        if entry.input == anchor_id
        for column in entry.columns
    }
    injected = find_template_fields(stage.llm.prompt_data_template)
    issues = [
        f"stage '{stage.id}': signature reads `{name}` but the prompt template "
        f"never injects it"
        for name in sorted(declared - injected)
    ]
    issues.extend(
        f"stage '{stage.id}': the prompt template injects {{{name}}} but the "
        f"signature does not read it"
        for name in sorted(injected - declared)
    )
    if signature.rewrites:
        issues.append(
            f"stage '{stage.id}': llm_transform passes its input through untouched "
            f"and only adds columns; rewrites are not supported"
        )
    return issues


def find_llm_prompt_column_issues(stage: "LLMTransformStage") -> list[str]:
    """Every way the prompt template's column references are wrong: a
    `{placeholder}` absent from the resolved input, or an input column that is
    double-braced and so never injected."""
    llm = stage.llm
    cols = resolve_input_columns(stage, 0)
    injected = find_template_fields(llm.prompt_data_template)
    issues = [
        COLUMN_ISSUE.format(sid=stage.id, field=f"llm prompt {{{field}}}", col=field, cols=sorted(cols))
        for field in sorted(injected)
        if field not in cols
    ]
    issues.extend(
        find_double_braced_input_issues(llm.prompt_data_template, injected, stage.inputs[0].table_schema)
    )
    return issues


def find_double_braced_input_issues(
    template: str, injected: set[str], input_schema: TableSchema
) -> list[str]:
    """`{{col}}` is an escaped literal under str.format_map — it renders as the
    text `{col}` and the row's value never reaches the model. Double-bracing a
    REAL input column is therefore always a mistake: the author meant to inject
    it. Reject exactly that. A prompt that injects nothing is unusual but
    allowed, so this requires no injection — only that a named input column is
    not escaped. Independent of the 1:1 grain contract: this is prompt wiring,
    not schema shape."""
    double_braced = [
        column.name for column in input_schema.columns
        if column.name not in injected
        and re.search(r"\{\{\s*" + re.escape(column.name) + r"\s*\}\}", template)
    ]
    if not double_braced:
        return []
    return [
        f"llm_transform prompt_data_template double-braces input column(s) "
        f"{sorted(double_braced)}: str.format_map treats double braces as an "
        f"escaped literal and never injects the value. Use single braces "
        f"around the column name."
    ]


def find_llm_one_to_one_issues(stage: "LLMTransformStage") -> list[str]:
    """An llm_transform maps one input row to one output row, so on its
    DECLARED schemas alone it must: take exactly one input; keep every input
    column unchanged (a transform never rewrites an existing column's
    schema); and add at least one new column.

    Checked so the reply spec the runtime computes
    (`output_schema.subtract(input_schema)`) is exactly the added columns and
    can never throw mid-run."""
    if len(stage.inputs) != 1:
        return [f"llm_transform must have exactly one input, has {len(stage.inputs)}"]
    input_schema = stage.inputs[0].table_schema
    output_schema = stage.resolve_output_schema()
    assert output_schema is not None  # _schemas_declared: an outer is stored or resolves
    return _find_additive_shape_issues(input_schema, output_schema)


# Helper for find_llm_one_to_one_issues: it has already confirmed
# `input_schema`/`output_schema` are both declared before calling it.


def _find_additive_shape_issues(input_schema: TableSchema, output_schema: TableSchema) -> list[str]:
    issues: list[str] = []
    if not input_schema.is_subset_of(output_schema):
        issues.append(
            "output must keep every input column unchanged (a transform is "
            f"additive: output ⊇ input); input columns "
            f"{[c.name for c in input_schema.columns]} vs output columns "
            f"{[c.name for c in output_schema.columns]}"
        )
    input_names = {c.name for c in input_schema.columns}
    if not any(c.name not in input_names for c in output_schema.columns):
        issues.append("output_schema adds no columns beyond the input")
    return issues

# Authoring copy for this module's stage type(s); assembled into NODE_TYPES.
NODE_TYPE_SPECS: dict[str, NodeTypeSpec] = {
    "llm_transform": NodeTypeSpec(
        summary="Row-by-row LLM call producing structured output.",
        signature_form="extends",
        blocks=["llm"],
        requires_inputs=True,
        min_inputs=1,
        required=["prompt_data_template"],
        optional=["model", "temperature", "response_format", "max_retries",
                     "rubric", "tools"],
        notes=(
            "Author it as TWO fields: prompt_instructions is the row-invariant guidance "
            "(role, methodology, how to weigh evidence/sources) and MUST NOT depend on "
            "any row value — the same instructions run over every input row, so keeping "
            "them byte-stable and separate from per-row data lets the runtime cache that "
            "prefix, cutting latency (and cost on a per-token backend). "
            "prompt_data_template is the minimal per-row input framing, rendered with "
            "Python's str.format_map: inject a column as {column_name}. "
            "Its output_schema must be strictly ADDITIVE and 1:1: every input column "
            "unchanged, plus at least one new column (one input row -> one output row)."
        ),
    ),
}

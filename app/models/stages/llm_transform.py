"""llm_transform stage: the config block, the prompt-wiring checks (every
`{placeholder}` resolves against what the input supplies, and no input column is
double-braced), and the 1:1 additive contract its signature must meet."""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Optional, Sequence

from pydantic import AliasChoices, Field, model_validator

from app.core.llm.options import LLMModel
from app.core.prompt_template import find_template_fields
from app.models.schema import StageConfig, TableSchema
from app.models.stages.stage_base import AbstractStage, StageInput, StageType
from app.models.stages.shared import COLUMN_ISSUE, resolve_input_columns
from app.models.stages.stage_type_spec import StageTypeSpec
from app.models.stages.signature import ExtendsSignature
from app.models.stages.warnings import CompilerWarning, warn

if TYPE_CHECKING:
    from app.models.workflow_stage import WorkflowStageInput


# What a STORED stage may carry: the typo check, not the grant list.
KNOWN_TOOL_NAMES: frozenset[str] = frozenset({
    "WebSearch", "WebFetch", "Bash",
})

# What a NEW stage may ask for; app.services.stage_edit refuses the rest.
GRANTABLE_TOOLS: frozenset[str] = frozenset({"WebSearch", "WebFetch"})


# How much the model reasons before it answers. A LEVEL, not a token budget:
# `budget_tokens` is removed on every current frontier model and returns 400
# there, so a number stored in a workflow would break the day the stage's model
# is repointed. These two names are the API's own and map straight through.
ThinkingMode = Literal["adaptive", "disabled"]


class LLMConfig(StageConfig):
    FINGERPRINT_FIELDS: ClassVar[frozenset[str]] = frozenset({
        "prompt_instructions", "prompt_data_template", "model", "temperature",
        "max_retries", "response_format", "rubric", "tools", "batch_size",
        "thinking",
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
    model: Optional[LLMModel] = Field(
        default=None,
        description=(
            "Which model answers. Name one on every stage you write: the run records "
            "it beside the rows, so a reader can see what produced them. A stage that "
            "names none is answered by whatever the deployment defaults to that day, "
            "and the write path refuses it."
        ),
    )
    temperature: float = 0.0
    max_retries: int = 3
    response_format: Literal["json", "text"] = "json"
    rubric: Optional[dict[str, Any]] = None
    tools: Optional[list[str]] = Field(
        default=None,
        description=(
            "Tools this stage may use: `WebSearch`, `WebFetch`. Omit for a stage that "
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

    thinking: Optional[ThinkingMode] = Field(
        default=None,
        description=(
            "How much the model reasons before answering. Omit to leave the "
            "backend's own setting. `disabled` is worth choosing when the answer is "
            "one value from a short enum and nobody reads the reasoning — it was 90% "
            "of one classifier stage's bill. It also CHANGES ANSWERS, so it is a "
            "judgment about the stage, not only about cost."
        ),
    )

    @model_validator(mode="after")
    def _tools_are_known_names(self) -> "LLMConfig":
        if not self.tools:
            return self
        unknown = sorted(set(self.tools) - KNOWN_TOOL_NAMES)
        if unknown:
            # Typo protection: a misspelled name would otherwise grant nothing and
            # leave the stage quietly unable to do the work it was authored to do.
            raise ValueError(
                f"llm.tools: unknown tool name(s) {unknown}; known names are "
                f"{sorted(KNOWN_TOOL_NAMES)} (names are case-sensitive)."
            )
        if self.batch_size > 1:
            # Batch-mates share one context, so one row's research would contaminate
            # the next one's answer. Research rows must be judged independently.
            raise ValueError(
                "llm.tools requires batch_size=1: batched rows share a context, so "
                "one row's findings would leak into another's answer."
            )
        return self


class LLMTransformStage(AbstractStage):
    type: Literal[StageType.llm_transform]
    llm: LLMConfig
    # Exactly one input, like every other row-mapped type: the runtime maps the
    # prompt over ONE frame's rows, so a second input names no rows to map.
    inputs: list[StageInput] = Field(default_factory=list, min_length=1, max_length=1)
    signature: ExtendsSignature
    # On, against the default: a re-run of this stage re-samples the model, so
    # replaying the recorded answer is what makes the row reproducible — and it
    # is the one stage type whose recompute is billed per row.
    cache: bool = True

    def fingerprint_blocks(self) -> dict[str, StageConfig]:
        return {"llm": self.llm}

    def find_handle_compiler_warnings(self) -> list[CompilerWarning]:
        return find_llm_warnings(self)

    def find_config_column_issues(
        self, inputs: Sequence["WorkflowStageInput"]
    ) -> list[str]:
        return find_llm_prompt_column_issues(self, inputs)

    def find_signature_config_issues(self) -> list[str]:
        return find_llm_signature_issues(self)


def find_llm_warnings(stage: "LLMTransformStage") -> list[CompilerWarning]:
    if stage.cache:
        return []
    return [warn(stage, "nondeterministic",
                 "caching is off, so every run re-samples the model and nothing an "
                 "earlier run answered pins what this stage produces")]


def find_llm_signature_issues(stage: "LLMTransformStage") -> list[str]:
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
    if not signature.adds:
        issues.append(f"stage '{stage.id}': the signature adds no columns beyond the "
                      f"input, so the model is asked for nothing")
    return issues


def find_llm_prompt_column_issues(
    stage: "LLMTransformStage", inputs: Sequence["WorkflowStageInput"]
) -> list[str]:
    llm = stage.llm
    cols = resolve_input_columns(inputs, 0)
    injected = find_template_fields(llm.prompt_data_template)
    issues = [
        COLUMN_ISSUE.format(sid=stage.id, field=f"llm prompt {{{field}}}", col=field, cols=sorted(cols))
        for field in sorted(injected)
        if field not in cols
    ]
    issues.extend(
        find_double_braced_input_issues(
            llm.prompt_data_template, injected, inputs[0].table_schema
        )
    )
    return issues


def find_double_braced_input_issues(
    template: str, injected: set[str], input_schema: TableSchema
) -> list[str]:
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



# Authoring copy for this module's stage type(s); assembled into STAGE_TYPES.
STAGE_TYPE_SPECS: dict[str, StageTypeSpec] = {
    "llm_transform": StageTypeSpec(
        summary="Row-by-row LLM call producing structured output.",
        signature_form="extends",
        blocks=["llm"],
        requires_inputs=True,
        min_inputs=1,
        required=["prompt_data_template", "model"],
        optional=["temperature", "response_format", "max_retries",
                     "rubric", "tools"],
        notes=(
            "Author it as TWO fields: prompt_instructions is the row-invariant guidance "
            "(role, methodology, how to weigh evidence/sources) and MUST NOT depend on "
            "any row value — the same instructions run over every input row, so keeping "
            "them byte-stable and separate from per-row data lets the runtime cache that "
            "prefix, cutting latency (and cost on a per-token backend). "
            "prompt_data_template is the minimal per-row input framing, rendered with "
            "Python's str.format_map: inject a column as {column_name}. "
            "Strictly ADDITIVE: the signature adds at least one column."
        ),
    ),
}

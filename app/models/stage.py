"""Stage-level contract: the node types, their executable-handle blocks, and the
Stage model. Constructing a model validates it.

Models ignore unknown keys (compiled stage JSON carries fields we pass through) but are
strict about the fields declared here.
"""
from __future__ import annotations

import json
import re
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar, Literal, Optional

from pydantic import AliasChoices, Field, ValidationError, field_validator, model_validator

from app.core.llm.options import LLMModel
from app.models.schema import (
    SourceRef,
    TableSchema,
    _Base,
    _SNAKE_RE,
)
from app.models.stages.code import validate_inline_function_code
from app.models.stages.stage_tests import StageTest, validate_stage_tests
from app.core.prompt_template import find_template_fields
from app.core.utils import compute_short_hash, format_errors

# ── Enumerated vocabularies ──────────────────────────────────────────────────
class StageType(str, Enum):
    input_data = "input_data"
    llm_transform = "llm_transform"
    # Two Python transforms, distinguished by how the runtime invokes them (which
    # is what makes the grain guarantee real rather than a claim):
    #   python_row_function   — runtime maps the function over the input's rows,
    #                           one row in → one row out. It never sees the frame,
    #                           so it *cannot* fan out / fan in. Prefer this.
    #   python_frame_function — runtime hands it the whole frame(s); it may reshape
    #                           (group-by, pivot, dedup, multi-input merge).
    python_row_function = "python_row_function"
    python_frame_function = "python_frame_function"
    # Trailing underscore: a member literally named `join` would shadow
    # str.join on every instance. The value — what a compiled stage declares and
    # StageType("join") looks up — is still "join".
    join_ = "join"
    aggregate = "aggregate"
    human_review_queue = "human_review_queue"
    publish = "publish"


# The stage types that guarantee output row i came from input row i — 1:1 and in
# the same order. This is owned by core (app.models) because it's a fact about the
# stage-type contract that several layers must read — the eval gate, the SYW
# positional tracer, the compiler — and only core is importable by all of them.
# Ask it through is_grain_and_order_preserving(); the runtime handler registry is
# held to conform (app/runtime/stages checks each type's shape against it at import).
_GRAIN_AND_ORDER_PRESERVING_TYPES: frozenset[StageType] = frozenset({
    StageType.input_data,
    StageType.python_row_function,
    StageType.llm_transform,
})


def is_grain_and_order_preserving(stage_type: StageType) -> bool:
    """Does one input row of this stage type map to exactly one output row, in the
    same order? Fixed entirely by stage type — see the
    Stage.is_grain_and_order_preserving property for the per-type contract."""
    return stage_type in _GRAIN_AND_ORDER_PRESERVING_TYPES


class ConnectorKind(str, Enum):
    file = "file"


class FileFormat(str, Enum):
    csv = "csv"
    parquet = "parquet"
    json = "json"
    geojson = "geojson"


class AggFormula(str, Enum):
    sum = "sum"
    mean = "mean"
    count_ = "count"  # trailing underscore: `count` would shadow str.count
    min = "min"
    max = "max"
    first = "first"
    list = "list"


class JoinType(str, Enum):
    inner = "inner"
    left = "left"
    right = "right"
    outer = "outer"


class FunctionKind(str, Enum):
    inline = "inline"
    module = "module"


class PublishFormat(str, Enum):
    html_report = "html_report"
    json = "json"
    csv = "csv"
    evidence_cards = "evidence_cards"


# ── Executable-handle blocks (each self-validates) ───────────────────────────
class Connector(_Base):
    """input_data handle."""
    # Every field changes what this stage computes (which file, what params) —
    # see Stage.compute_definition_fingerprint.
    FINGERPRINT_FIELDS: ClassVar[frozenset[str]] = frozenset({"kind", "params", "refresh", "notes"})
    INCIDENTAL_FIELDS: ClassVar[frozenset[str]] = frozenset()

    kind: ConnectorKind
    params: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Connector parameters. For kind=file: params.path, when present, is the "
            "ABSOLUTE path to the data file, plus optional params.format "
            "(csv/parquet/json/geojson). If the source material does not state where "
            "the file lives, OMIT path entirely — the user binds a file when starting "
            "a run. Never invent a path."
        ),
    )
    refresh: str = "ad_hoc"
    notes: Optional[str] = None

    @model_validator(mode="after")
    def _params_for_kind(self) -> "Connector":
        if self.kind == ConnectorKind.file:
            path = (self.params or {}).get("path")
            if path is not None:
                if not isinstance(path, str) or not path.strip():
                    raise ValueError("connector params.path must be a non-empty string when present")
                if not Path(path).is_absolute():
                    raise ValueError(f"connector params.path must be an ABSOLUTE path, got {path!r}")
            fmt = (self.params or {}).get("format")
            if fmt is not None and fmt not in {f.value for f in FileFormat}:
                raise ValueError(f"unknown file format {fmt!r}")
        return self


class LLMConfig(_Base):
    """llm_transform handle."""
    # Every field changes what this stage computes (the prompt, the model, the
    # sampling/response knobs) — see Stage.compute_definition_fingerprint.
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
            "Row-invariant guidance belongs in prompt_instructions instead, so "
            "this stays a stable, cacheable prompt prefix across many rows."
        ),
    )
    model: Optional[LLMModel] = None
    temperature: float = 0.0
    max_retries: int = 3
    response_format: Literal["json", "text"] = "json"
    rubric: Optional[dict[str, Any]] = None
    tools: Optional[list[str]] = None
    batch_size: int = Field(
        default=1,
        ge=1,
        description=(
            "Rows sent per model call. 1 (default) calls the model once per row. "
            ">1 packs that many input rows into one call, amortizing the prompt/"
            "harness overhead; the runtime tags each row with a batch-local row "
            "number and rejoins the replies by it, so the stage stays strictly "
            "one-row-out-per-row-in — but the model sees a whole chunk at once, so "
            "batch_size>1 relaxes per-row independence (a row's answer can be "
            "influenced by its batch-mates)."
        ),
    )


class PythonFunction(_Base):
    """Handle for python_row_function / python_frame_function (and publish). The
    row-vs-frame distinction lives in the stage `type`, not here — the runtime
    reads the type to decide whether to invoke this per row or per frame."""
    # Every field changes what this stage computes (the code/module it runs) —
    # see Stage.compute_definition_fingerprint.
    FINGERPRINT_FIELDS: ClassVar[frozenset[str]] = frozenset({
        "kind", "code", "module", "function", "requirements",
    })
    INCIDENTAL_FIELDS: ClassVar[frozenset[str]] = frozenset()

    kind: FunctionKind
    code: Optional[str] = Field(
        default=None,
        description=(
            "Inline Python defining the function named by `function` (default `transform`). "
            "Its signature depends on the stage type: "
            "python_row_function -> `def transform(row: dict) -> dict` (one row in, one row "
            "out; cannot fan out/in or reorder); "
            "python_frame_function -> `def transform(df, ...) -> DataFrame` (the input pandas "
            "DataFrame(s), positional in declared input order; may reshape); "
            "publish -> `def transform(df, ..., output_dir, trace_links) -> DataFrame` "
            "(write artifacts under output_dir, return a table of their paths; declare "
            "`trace_links` to receive a linker that builds each row's provenance URL)."
        ),
    )
    module: Optional[str] = None
    function: Optional[str] = Field(
        default=None,
        description="Name of the top-level function the runtime calls (default `transform`).",
    )
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


class JoinKey(_Base):
    left: str
    right: str


class JoinConfig(_Base):
    """join handle. `keys` OR `on` is accepted.

    The merged output contains: every LEFT column under its own name; each
    RIGHT column under its own name unless a left column shares it, in which
    case it appears as `<name>_r`; a key pair with the SAME name on both sides
    collapses into one column (there is no `<key>_r`). `select` and the
    stage's `output_schema` may only name these producible columns — anything
    else is rejected when the stage is saved."""
    # Every field changes what this stage computes (join type, keys, kept
    # columns) — see Stage.compute_definition_fingerprint.
    FINGERPRINT_FIELDS: ClassVar[frozenset[str]] = frozenset({"type", "keys", "on", "select"})
    INCIDENTAL_FIELDS: ClassVar[frozenset[str]] = frozenset()

    type: JoinType = JoinType.inner
    keys: Optional[list[JoinKey]] = None
    on: Optional[list[JoinKey]] = None
    select: Optional[list[str]] = Field(
        default=None,
        description=(
            "Columns to keep, applied after the merge. Each entry must be a "
            "producible merged column: a left column name, an uncollided right "
            "column name, or `<name>_r` for a right column whose name a left "
            "column shares."
        ),
    )

    @model_validator(mode="after")
    def _need_keys_or_on(self) -> "JoinConfig":
        if not (self.keys or self.on):
            raise ValueError("join needs `keys` or `on`")
        return self


class AggregationOp(_Base):
    output_column: str
    formula: AggFormula
    value_column: Optional[str] = None
    where: Optional[str] = None

    @model_validator(mode="after")
    def _value_column_for_formula(self) -> "AggregationOp":
        if self.formula != AggFormula.count_ and not self.value_column:
            raise ValueError(
                f"aggregation `{self.output_column}`: formula `{self.formula}` needs value_column"
            )
        return self


class AggregateConfig(_Base):
    """aggregate handle."""
    # Every field changes what this stage computes (grouping, aggregations) —
    # see Stage.compute_definition_fingerprint.
    FINGERPRINT_FIELDS: ClassVar[frozenset[str]] = frozenset({"group_by", "aggregations"})
    INCIDENTAL_FIELDS: ClassVar[frozenset[str]] = frozenset()

    group_by: list[str]
    aggregations: list[AggregationOp]


class RowReviewDecision(str, Enum):
    """A reviewer's verdict on one human_review_queue row, validated and applied
    at the web/service boundary (app.services.review) and recorded as the review
    stage's output row in the cache: `approve` keeps the AI score as final,
    `modify` substitutes a human-entered score, `reject` drops the row from the
    stage's output."""
    approve = "approve"
    modify = "modify"
    reject = "reject"


class QueueConfig(_Base):
    """human_review_queue handle. A queued row is matched to a cached human
    decision by fingerprinting the row itself (app.core.stage_cache) — no
    column configuration is needed to enable that matching."""
    # `filter`/`reviewer_instructions` change what the human is asked; routing,
    # conflict_resolution, and estimated_volume_per_week describe how a
    # decision is routed, not what is asked — see
    # Stage.compute_definition_fingerprint.
    FINGERPRINT_FIELDS: ClassVar[frozenset[str]] = frozenset({"filter", "reviewer_instructions"})
    INCIDENTAL_FIELDS: ClassVar[frozenset[str]] = frozenset({
        "routing", "conflict_resolution", "estimated_volume_per_week",
    })

    filter: Optional[str] = None
    reviewer_instructions: Optional[str] = None
    routing: Optional[str] = None
    conflict_resolution: Optional[str] = None
    estimated_volume_per_week: Optional[int] = None


class PublishConfig(_Base):
    """publish handle (runs alongside a `function` block)."""
    # Every field changes what this stage computes (format, destination,
    # template, layout) — see Stage.compute_definition_fingerprint.
    FINGERPRINT_FIELDS: ClassVar[frozenset[str]] = frozenset({
        "format", "destination", "template", "one_file_per", "cross_link",
    })
    INCIDENTAL_FIELDS: ClassVar[frozenset[str]] = frozenset()

    format: Optional[PublishFormat] = None
    destination: Optional[str] = None
    template: Optional[str] = None
    one_file_per: Optional[str] = None
    cross_link: Optional[bool] = None


class ReviewConfig(_Base):
    """Routes a stage's outputs into human review."""
    when: Optional[str] = None
    routing: Optional[str] = None
    rationale: Optional[str] = None
    queue_name: Optional[str] = None


class InputRef(_Base):
    """One upstream dependency: the upstream stage id, plus (optionally) the
    schema this stage expects that upstream's output to satisfy. The runner
    validates the real dataframe against `table_schema` before the stage runs.
    The compiled stage spells the field `schema:`; the python name differs only
    because pydantic reserves `schema` on BaseModel."""
    id: str
    table_schema: Optional[TableSchema] = Field(default=None, alias="schema")


# ── Stage ────────────────────────────────────────────────────────────────────
# type → which handle block it must carry, plus input arity. Keyed by the plain
# value string, not the enum member: with `use_enum_values`, `self.type` is a
# str at runtime, and str-enum members hash by *name* (StageType.join_ hashes
# as "join_", not "join") — a member-keyed dict would silently miss the lookup.
_TYPE_SPEC: dict[str, dict[str, Any]] = {
    "input_data":            {"handle": "connector", "requires_inputs": False, "min_inputs": 0},
    "llm_transform":         {"handle": "llm",       "requires_inputs": True,  "min_inputs": 1},
    "python_row_function":   {"handle": "function",  "requires_inputs": True,  "min_inputs": 1, "max_inputs": 1},
    "python_frame_function": {"handle": "function",  "requires_inputs": True,  "min_inputs": 1},
    "join":                  {"handle": "join",      "requires_inputs": True,  "min_inputs": 2},
    "aggregate":             {"handle": "aggregate", "requires_inputs": True,  "min_inputs": 1},
    "human_review_queue":    {"handle": "queue",     "requires_inputs": True,  "min_inputs": 1},
    "publish":               {"handle": "publish",   "also_requires": ["function"], "requires_inputs": True, "min_inputs": 1},
}


class Stage(_Base):
    """One node in the workflow. Exactly one handle block is required,
    selected by `type`."""
    id: str
    type: StageType
    name: str
    source: Optional[SourceRef] = None
    inputs: list[InputRef] = Field(
        default_factory=list,
        description=(
            "Upstream dependencies: each is an upstream stage id plus, optionally, the schema "
            "this stage expects that input to satisfy — which is just the upstream stage's "
            "output_schema. An llm_transform's single input must declare a primary_key."
        ),
    )
    output_schema: Optional[TableSchema] = Field(
        default=None,
        description=(
            "Columns this stage outputs, with an optional primary_key. For an llm_transform "
            "this must be strictly ADDITIVE and 1:1: declare the SAME primary_key as its "
            "single input's schema, keep every input column unchanged, and add at least one "
            "new column (one input row -> one output row)."
        ),
    )

    # executable handles (exactly one populated, per type)
    connector: Optional[Connector] = None
    llm: Optional[LLMConfig] = None
    function: Optional[PythonFunction] = None
    join: Optional[JoinConfig] = None
    aggregate: Optional[AggregateConfig] = None
    queue: Optional[QueueConfig] = None
    publish: Optional[PublishConfig] = None

    review: Optional[ReviewConfig] = None
    limit: Optional[int] = None
    compiler_notes: list[str] = Field(default_factory=list)

    # Descriptive eval note rendered on the stage page (reference data, metrics).
    # Display only — the executable eval contract is EvalConfig (app/models/eval.py).
    eval: Optional[dict[str, Any]] = None

    # Authored input→expected-output cases for python transforms — the stage's
    # reviewable behavior contract, run by app.runtime.stage_tests. None when the
    # stage has none: the canonical dump must not carry a `tests` key for
    # stages without tests, or every pre-existing belief hash would change.
    tests: Optional[list[StageTest]] = None

    @field_validator("inputs", mode="before")
    @classmethod
    def _bare_id_shorthand(cls, v: Any) -> Any:
        """Accept `inputs: [upstream_id]` shorthand for `[{id: upstream_id}]`."""
        if not isinstance(v, list):
            return v
        return [{"id": item} if isinstance(item, str) else item for item in v]

    @property
    def input_ids(self) -> list[str]:
        return [ref.id for ref in self.inputs]

    def compute_definition_fingerprint(self) -> str:
        """sha1[:16] over the canonical JSON of the output-determining subset of
        this stage: {"type", "handle": <the type's handle block>, "output_schema"}.
        Every other Stage field (id, name, source, inputs, review, limit,
        compiler_notes, eval, tests) is incidental — it does not change what
        this stage computes — and stays out of the fingerprint. The handle
        block itself is trimmed to its class's own `FINGERPRINT_FIELDS` (every
        handle config class declares `FINGERPRINT_FIELDS`/`INCIDENTAL_FIELDS`
        explicitly, exhaustively over its own fields — see e.g. QueueConfig,
        whose `routing`/`conflict_resolution`/`estimated_volume_per_week`
        route or match a decision without changing what the human is asked)."""
        spec = _TYPE_SPEC[self.type]
        handle = getattr(self, spec["handle"])
        handle_dump = handle.model_dump(mode="json", exclude_none=True)
        handle_dump = {
            key: value for key, value in handle_dump.items()
            if key in type(handle).FINGERPRINT_FIELDS
        }
        output_dump = (
            self.output_schema.model_dump(mode="json", exclude_none=True)
            if self.output_schema is not None else None
        )
        canonical = {"type": self.type, "handle": handle_dump, "output_schema": output_dump}
        payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str)
        return compute_short_hash(payload)

    @field_validator("id")
    @classmethod
    def _snake_case(cls, v: str) -> str:
        if not _SNAKE_RE.match(v):
            raise ValueError(f"id {v!r} should be snake_case")
        return v

    @field_validator("tests", mode="before")
    @classmethod
    def _empty_tests_means_absent(cls, v: Any) -> Any:
        """Normalise `tests: []` to absent, so the canonical dump (and the
        belief hash computed over it) is identical whether the key was omitted
        or given empty."""
        return None if v == [] else v

    @model_validator(mode="after")
    def _tests_shape(self) -> "Stage":
        validate_stage_tests(self.type, self.input_ids, self.tests or [])
        return self

    @model_validator(mode="after")
    def _handle_for_type(self) -> "Stage":
        spec = _TYPE_SPEC[self.type]
        handle = spec["handle"]
        if getattr(self, handle) is None:
            raise ValueError(f"type `{self.type}` requires a `{handle}:` block")
        for extra in spec.get("also_requires", ()):
            if getattr(self, extra) is None:
                raise ValueError(f"type `{self.type}` also requires a `{extra}:` block")
        if spec["requires_inputs"] and len(self.inputs) < spec["min_inputs"]:
            raise ValueError(
                f"type `{self.type}` needs >= {spec['min_inputs']} input(s), got {len(self.inputs)}"
            )
        max_inputs = spec.get("max_inputs")
        if max_inputs is not None and len(self.inputs) > max_inputs:
            raise ValueError(
                f"type `{self.type}` takes <= {max_inputs} input(s), got {len(self.inputs)} "
                f"(more than one input is a join, or use python_frame_function)"
            )
        return self

    @model_validator(mode="after")
    def _llm_transform_one_to_one(self) -> "Stage":
        """An llm_transform maps one input row to one output row, so on its
        DECLARED schemas alone it must: take exactly one input; declare a
        primary_key on both that input's schema and its output_schema, naming
        the same columns; keep every input column unchanged (a transform never
        rewrites an existing column's schema); and add at least one new column.

        Enforced here — a stage carries its own contract — so the reply spec the
        runtime derives (`output_schema.subtract(input_schema)`) is exactly the
        added columns and can never throw mid-run. Cross-stage checks (unique
        ids, inputs resolve, acyclic) live in `workflow.graph_issues`; a single
        stage's invariants live on the stage."""
        if self.type != StageType.llm_transform:
            return self

        if len(self.inputs) != 1:
            raise ValueError(
                f"llm_transform must have exactly one input, has {len(self.inputs)}"
            )
        input_schema = self.inputs[0].table_schema
        output_schema = self.output_schema
        if input_schema is None or output_schema is None:
            missing = "input schema" if input_schema is None else "output_schema"
            raise ValueError(
                f"llm_transform declares no {missing}; a 1:1 stage needs a "
                "primary_key on both its input and output schemas"
            )

        issues = _find_primary_key_issues(input_schema, output_schema)
        issues.extend(_find_additive_shape_issues(input_schema, output_schema))
        if issues:
            raise ValueError("llm_transform not strictly 1:1: " + "; ".join(issues))
        return self

    @model_validator(mode="after")
    def _llm_prompt_no_double_braced_input(self) -> "Stage":
        """An llm_transform's prompt_data_template is rendered with str.format_map,
        where `{{col}}` is an escaped literal (renders as the text `{col}`) — never
        the row's value. Double-bracing a REAL input column is therefore always a
        mistake: the author meant to inject it, but the data silently never reaches
        the model. Reject exactly that. A prompt that injects nothing is unusual but
        allowed, so this does not require any injection — only that a named input
        column is not escaped. Independent of the 1:1 grain contract: this is prompt
        wiring, not schema shape."""
        if self.type != StageType.llm_transform or self.llm is None:
            return self
        input_schema = self.inputs[0].table_schema if self.inputs else None
        if input_schema is None:
            return self
        template = self.llm.prompt_data_template
        injected = find_template_fields(template)
        double_braced = [
            column.name for column in input_schema.columns
            if column.name not in injected
            and re.search(r"\{\{\s*" + re.escape(column.name) + r"\s*\}\}", template)
        ]
        if double_braced:
            raise ValueError(
                f"llm_transform prompt_data_template double-braces input column(s) "
                f"{sorted(double_braced)}: str.format_map treats double braces as an "
                f"escaped literal and never injects the value. Use single braces "
                f"around the column name."
            )
        return self

    @model_validator(mode="after")
    def _config_columns_resolve(self) -> "Stage":
        """Every column this stage's config directly names (a join key, an
        aggregate group_by/value_column, publish.one_file_per, an llm prompt
        {placeholder}) or references via a where/filter predicate (aggregate
        `where`, human_review_queue `filter`) must resolve against that
        reference's own input edge — `inputs[index].table_schema`, per
        `app.models.stages.shared.resolve_input_columns`. EDGE-ONLY: this says
        nothing about what an upstream producer itself declares, so it holds
        for a single stage in isolation, independent of the rest of any
        workflow. A reference whose edge declares no schema at all is skipped,
        not flagged — unresolvable means unknowable, never wrong.

        Runs after `_handle_for_type`, so the type-matched handle block (join/
        aggregate/publish/llm/queue) this dispatches on is already guaranteed
        present. Lazy-imports the dispatch, rather than importing it at module
        level like every other import in this file: `app.models.stages`
        needs `Stage` back only for a type hint, but a module-level import
        here would run while this module (which defines `Stage`) is still
        mid-import."""
        from app.models.stages import find_config_column_issues

        issues = find_config_column_issues(self)
        if issues:
            raise ValueError("; ".join(issues))
        return self

    @model_validator(mode="after")
    def _output_schema_deliverable(self) -> "Stage":
        """A declared output_schema must be deliverable by this stage's own
        handle: for the types whose output is fixed by config (join, aggregate),
        every declared column must be producible by name, with the declared type
        matching the derivation where it can be known (see
        app.models.stages.find_output_schema_issues). EDGE-ONLY and per-stage,
        like _config_columns_resolve; same lazy import, same reason."""
        from app.models.stages import find_output_schema_issues

        issues = find_output_schema_issues(self)
        if issues:
            raise ValueError("; ".join(issues))
        return self

    @property
    def is_grain_and_order_preserving(self) -> bool:
        """Does one input row map to exactly one output row, IN THE SAME ORDER?
        Grain-preserving means both: 1:1 (no rows added or dropped) AND order-
        preserving (the Nth output row was produced from the Nth input row).
        Declaring a type grain-preserving commits it to both — a stage that
        reordered rows would break the guarantee even at 1:1.

        This is the v1 eval gate AND the property a declarative (single-table,
        row-aligned) eval relies on to align a target's output rows back to the
        eval-dataset rows that produced them BY POSITION — no lineage id needed,
        because position IS the identity through a grain-preserving path. Fixed
        entirely by stage type (the module function is_grain_and_order_preserving):
          - python_row_function → yes (runtime maps it per row, in emit order — enforced 1:1)
          - python_frame_function → NO (may reshape OR reorder the frame)
          - llm_transform      → yes (per-row 1:1 in emit order in v1; a fan-out LLM
                                 like doc→pieces is out of scope until fan-out evals)
          - input_data         → yes (originates the rows)
          - human_review_queue → NO — handle_human_review_queue drops rejected rows
                                 and concatenates decided+passthrough, changing both
                                 grain and order. Its intended "edits in place"
                                 contract would make it yes; closing that gap is #106.
          - join (fan-out) / aggregate (fan-in) → NO; grain changes are deferred
          - publish            → NO — handle_publish runs an authored function whose
                                 output is a table of artifact paths, not the input
                                 rows (and it is terminal — nothing downstream).
        """
        return is_grain_and_order_preserving(self.type)


# ── llm_transform's 1:1 contract ─────────────────────────────────────────────
# Helpers for Stage._llm_transform_one_to_one: it has already confirmed
# `input_schema`/`output_schema` are both declared before calling these.


def _find_primary_key_issues(input_schema: TableSchema, output_schema: TableSchema) -> list[str]:
    issues: list[str] = []
    input_pk, output_pk = input_schema.primary_key, output_schema.primary_key
    if not input_pk:
        issues.append("input schema declares no primary_key")
    if not output_pk:
        issues.append("output_schema declares no primary_key")
    if input_pk and output_pk and set(input_pk) != set(output_pk):
        issues.append(
            f"input primary_key {input_pk} != output primary_key {output_pk}"
        )
    return issues


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


def validate_stage(stage: dict[str, Any]) -> list[str]:
    """Non-fatal structural validation of one stage dict ([] means valid)."""
    try:
        Stage.model_validate(stage)
        return []
    except ValidationError as err:
        return format_errors(err)

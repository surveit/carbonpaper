"""enrich/expand stage: the shared join handle config, plus column validation on
both the input and output side — every join key's `.left`/`.right` must resolve
against its side's stage input edge; and a declared output_schema (plus
`select`) must be deliverable by the columns the join actually produces."""
from __future__ import annotations

from typing import Any, TYPE_CHECKING, ClassVar, Literal, Optional

from pydantic import Field

from app.models.schema import StageConfig, _Base
from app.models.stage_base import StageBase, StageInput, StageType
from app.models.stages.shared import (
    COLUMN_ISSUE,
    find_declared_vs_computed_issues,
    resolve_input_columns,
)
from app.models.stages.signature import ExtendsSignature

if TYPE_CHECKING:
    from app.models.schema import TableSchema


class JoinKey(_Base):
    left: str
    right: str


class JoinConfig(StageConfig):
    """enrich/expand handle. Cardinality lives in the stage TYPE, not here.

    The joined output contains: every LEFT column under its own name; each
    RIGHT column under its own name unless a left column shares it, in which
    case it appears as `<name>_r`; a key pair with the SAME name on both sides
    collapses into one column (there is no `<key>_r`). `select` and the
    stage's `output_schema` may only name these producible columns — anything
    else is rejected when the stage is saved."""
    # Every field changes what this stage computes (keys, kept columns) — see
    # Stage.compute_definition_fingerprint.
    FINGERPRINT_FIELDS: ClassVar[frozenset[str]] = frozenset({"keys", "select"})
    INCIDENTAL_FIELDS: ClassVar[frozenset[str]] = frozenset()

    keys: list[JoinKey] = Field(min_length=1)
    select: Optional[list[str]] = Field(
        default=None,
        description=(
            "Columns to keep, applied after the join. Each entry must be a "
            "producible joined column: a left column name, an uncollided right "
            "column name, or `<name>_r` for a right column whose name a left "
            "column shares."
        ),
    )


class JoinStage(StageBase):
    """enrich and expand differ only in the cardinality the runtime enforces —
    the config, the arity and the column rules are the same."""
    join: JoinConfig
    inputs: list[StageInput] = Field(default_factory=list, min_length=2, max_length=2)
    signature: Optional[ExtendsSignature] = None

    def fingerprint_blocks(self) -> dict[str, StageConfig]:
        return {"join": self.join}

    def find_config_column_issues(self) -> list[str]:
        return find_join_column_issues(self)

    def find_output_schema_issues(self) -> list[str]:
        return find_join_output_issues(self)

    def find_signature_config_issues(self) -> list[str]:
        return find_join_signature_issues(self)


class EnrichStage(JoinStage):
    type: Literal[StageType.enrich]


class ExpandStage(JoinStage):
    type: Literal[StageType.expand]


SELECT_UNPRODUCIBLE_ISSUE = (
    "stage '{sid}': join.select references column '{col}' that the {stype} "
    "cannot produce (producible columns: {cols})"
)


def find_join_column_issues(stage: "JoinStage") -> list[str]:
    """Every join key whose `.left`/`.right` names a column absent from its
    resolved side's input."""
    join = stage.join
    left = resolve_input_columns(stage, 0)
    right = resolve_input_columns(stage, 1)
    issues: list[str] = []
    for key in join.keys:
        if key.left not in left:
            issues.append(
                COLUMN_ISSUE.format(sid=stage.id, field="join key .left", col=key.left, cols=sorted(left))
            )
        if key.right not in right:
            issues.append(
                COLUMN_ISSUE.format(sid=stage.id, field="join key .right", col=key.right, cols=sorted(right))
            )
    return issues


def find_join_output_issues(stage: "JoinStage") -> list[str]:
    """Every declared output_schema column (and select entry) the join handle
    cannot deliver."""
    join = stage.join
    assert stage.output_schema is not None  # StageBase._schemas_declared guarantees this
    left = stage.inputs[0].table_schema
    right = stage.inputs[1].table_schema
    joined = compute_join_output_types(join, left, right)
    stage_type = str(stage.type)
    issues = [
        SELECT_UNPRODUCIBLE_ISSUE.format(
            sid=stage.id, col=entry, stype=stage_type, cols=sorted(joined)
        )
        for entry in join.select or []
        if entry not in joined
    ]
    effective = (
        {name: joined[name] for name in join.select if name in joined}
        if join.select else joined
    )
    issues.extend(
        find_declared_vs_computed_issues(stage.id, stage_type, stage.output_schema, effective)
    )
    return issues


SIGNATURE_ADD_UNPRODUCIBLE_ISSUE = (
    "stage '{sid}': signature adds `{col}` that the join cannot produce from its "
    "reference input (producible: {cols})"
)


def find_join_signature_issues(stage: "JoinStage") -> list[str]:
    """Keys must be read from their side, adds produced by the reference; rewrites are refused."""
    signature = stage.signature
    assert signature is not None  # find_signature_config_issues runs only with one
    subject, reference = stage.inputs[0], stage.inputs[1]
    reads_by_input = {
        entry.input: {column.name for column in entry.columns}
        for entry in signature.reads
    }
    issues = [
        f"stage '{stage.id}': join key .{side} `{name}` is not read from the "
        f"{role} input `{ref.id}`"
        for side, role, ref in (("left", "subject", subject), ("right", "reference", reference))
        for name in {getattr(key, side) for key in stage.join.keys}
        if name not in reads_by_input.get(ref.id, set())
    ]

    joined = compute_join_output_types(stage.join, subject.table_schema, reference.table_schema)
    subject_names = {column.name for column in subject.table_schema.columns}
    producible = {name: t for name, t in joined.items() if name not in subject_names}
    for column in signature.adds:
        if column.name not in producible:
            issues.append(SIGNATURE_ADD_UNPRODUCIBLE_ISSUE.format(
                sid=stage.id, col=column.name, cols=sorted(producible),
            ))
        elif producible[column.name] != column.type:
            issues.append(
                f"stage '{stage.id}': signature adds `{column.name}` as "
                f"{column.type!r} but the join produces {producible[column.name]!r}"
            )
    if signature.rewrites:
        issues.append(
            f"stage '{stage.id}': a join never revises a subject column; "
            f"rewrites are not supported"
        )
    return issues


def compute_join_output_types(
    join: "JoinConfig", left: "TableSchema", right: "TableSchema"
) -> dict[str, str]:
    """The columns the join handle emits, each mapped to its type — mirroring
    pandas merge(..., suffixes=("", "_r")): all left columns keep
    their names and types; a right key whose pair shares the left key's name
    collapses into that left column; every other right column keeps its name
    unless it collides with a left column, in which case it appears as
    <name>_r. `select` projection is NOT applied here — the caller decides."""
    collapsed_right_keys = {k.right for k in join.keys if k.left == k.right}
    joined: dict[str, str] = {c.name: c.type for c in left.columns}
    left_names = set(joined)
    for column in right.columns:
        if column.name in collapsed_right_keys:
            continue
        name = column.name if column.name not in left_names else f"{column.name}_r"
        joined[name] = column.type
    return joined

# Authoring notes for this module's stage type(s), as the plain-data shape the
# authoring prompts render. Assembled into NODE_TYPES by app.models.stages.
NODE_TYPE_SPECS: dict[str, dict[str, Any]] = {
    "enrich": {
        "summary": "Adds reference columns to each subject row; the reference must be unique on the key (many-to-one).",
        "blocks": ["join"],
        "requires_inputs": True,
        "min_inputs": 2,
        "required": ["keys"],
        "optional": ["select"],
        "notes": (
            "Takes EXACTLY TWO inputs: inputs[0] is the SUBJECT, inputs[1] is the REFERENCE. "
            "Row count and order come out unchanged, because the reference is required to hold "
            "at most ONE row per key: the runtime asks pandas to VERIFY that, so a reference "
            "that repeats a key FAILS THE RUN rather than silently multiplying rows. Use "
            "`expand` when the fan-out is intended. Every subject row survives — an unmatched "
            "one carries nulls for the reference columns — and an unmatched reference row is "
            "dropped. This stage NEVER drops a subject row: to drop rows (e.g. inner-join "
            "semantics), follow it with a `filter_rows` on a reference column being non-null, "
            "which records the row loss instead of hiding it. "
            "A reference column whose name a subject column shares arrives as `<name>_r`; a key "
            "pair with the SAME name on both sides collapses into one column. `select` and "
            "output_schema may name only columns the join produces — anything else is rejected "
            "when the stage is saved."
        ),
    },
    "expand": {
        "summary": "Joins reference rows into each subject row, fanning one subject row out to several (many-to-many).",
        "blocks": ["join"],
        "requires_inputs": True,
        "min_inputs": 2,
        "required": ["keys"],
        "optional": ["select"],
        "notes": (
            "Takes EXACTLY TWO inputs: inputs[0] is the SUBJECT, inputs[1] is the REFERENCE. "
            "The reference MAY hold several rows per key, so one subject row may come out as "
            "several — deliberate fan-out. Use `enrich` instead when the reference is meant to "
            "be unique on the key and a repeat is a bug you want caught. Every subject row "
            "survives — an unmatched one carries nulls for the reference columns — and an "
            "unmatched reference row is dropped. This stage NEVER drops a subject row: to drop "
            "rows (e.g. inner-join semantics), follow it with a `filter_rows` on a reference "
            "column being non-null, which records the row loss instead of hiding it. "
            "A reference column whose name a subject column shares arrives as `<name>_r`; a key "
            "pair with the SAME name on both sides collapses into one column. `select` and "
            "output_schema may name only columns the join produces — anything else is rejected "
            "when the stage is saved."
        ),
    },
}

"""enrich/expand stage: the shared join handle config, plus column validation on
both the input and output side — every join key's `.left`/`.right` must resolve
against its side's stage input edge; every `bring` entry must name a reference
column the subject does not already carry; and a declared output_schema must be
deliverable as the subject's columns plus the brought ones."""
from __future__ import annotations

from typing import Any, TYPE_CHECKING, ClassVar, Literal, Optional

from pydantic import Field, model_validator

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

    The joined output is the subject frame extended by `bring`: every subject
    column under its own name, then each brought reference column under its
    own name. A bring entry naming a column the subject already carries is
    refused, never renamed — rename it upstream on the reference side if both
    must survive. A key pair with the SAME name on both sides needs no bring
    entry: the subject's own key column already carries the matched value."""
    # Every field changes what this stage computes (keys, brought columns) —
    # see Stage.compute_definition_fingerprint.
    FINGERPRINT_FIELDS: ClassVar[frozenset[str]] = frozenset({"keys", "bring"})
    INCIDENTAL_FIELDS: ClassVar[frozenset[str]] = frozenset()

    keys: list[JoinKey] = Field(min_length=1)
    bring: list[str] = Field(
        min_length=1,
        description=(
            "The reference columns this join adds to each subject row, each "
            "under its own name. Every entry must exist on the reference input "
            "and be absent from the subject — a collision is refused, never "
            "renamed. On an unmatched subject row every brought column is "
            "null."
        ),
    )

    @model_validator(mode="after")
    def _no_duplicate_bring(self) -> "JoinConfig":
        seen: set[str] = set()
        for name in self.bring:
            if name in seen:
                raise ValueError(f"duplicate column {name!r} in join.bring")
            seen.add(name)
        return self


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


BRING_COLLISION_ISSUE = (
    "stage '{sid}': join.bring names '{col}', which the subject input "
    "'{subject}' already supplies — a collision is refused, never renamed; "
    "rename the reference column upstream or leave it un-brought"
)


def find_join_column_issues(stage: "JoinStage") -> list[str]:
    """Keys and `bring` entries their side's edge cannot satisfy; a bring colliding with the subject."""
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
    for name in join.bring:
        if name not in right:
            issues.append(
                COLUMN_ISSUE.format(sid=stage.id, field="join.bring", col=name, cols=sorted(right))
            )
        elif name in left:
            issues.append(BRING_COLLISION_ISSUE.format(
                sid=stage.id, col=name, subject=stage.inputs[0].id
            ))
    return issues


def find_join_output_issues(stage: "JoinStage") -> list[str]:
    """Every declared output_schema column the join handle cannot deliver."""
    assert stage.output_schema is not None  # StageBase._schemas_declared guarantees this
    computed = compute_join_output_types(
        stage.join, stage.inputs[0].table_schema, stage.inputs[1].table_schema
    )
    return find_declared_vs_computed_issues(
        stage.id, str(stage.type), stage.output_schema, computed
    )


def find_join_signature_issues(stage: "JoinStage") -> list[str]:
    """Keys must be read from their side, adds must be exactly `bring`; rewrites are refused."""
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

    brought = set(stage.join.bring)
    reference_types = {c.name: c.type for c in reference.table_schema.columns}
    for column in signature.adds:
        if column.name not in brought:
            issues.append(
                f"stage '{stage.id}': signature adds `{column.name}`, which "
                f"join.bring does not bring (bring: {sorted(brought)})"
            )
        elif reference_types.get(column.name, column.type) != column.type:
            issues.append(
                f"stage '{stage.id}': signature adds `{column.name}` as "
                f"{column.type!r} but the reference supplies "
                f"{reference_types[column.name]!r}"
            )
    added = {column.name for column in signature.adds}
    issues.extend(
        f"stage '{stage.id}': join.bring brings `{name}` but the signature does "
        f"not add it"
        for name in stage.join.bring
        if name not in added
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
    """The columns the join handle emits, each mapped to its type: every left
    column under its own name and type, then each `bring` entry the right edge
    supplies under its own name with the right column's type. A bring entry the
    right edge does not supply, or the left side already does, contributes
    nothing here — `find_join_column_issues` reports it."""
    right_types = {c.name: c.type for c in right.columns}
    joined: dict[str, str] = {c.name: c.type for c in left.columns}
    for name in join.bring:
        if name in right_types and name not in joined:
            joined[name] = right_types[name]
    return joined

# Authoring notes for this module's stage type(s), as the plain-data shape the
# authoring prompts render. Assembled into NODE_TYPES by app.models.stages.
NODE_TYPE_SPECS: dict[str, dict[str, Any]] = {
    "enrich": {
        "summary": "Adds brought reference columns to each subject row; the reference must be unique on the key (many-to-one).",
        "blocks": ["join"],
        "requires_inputs": True,
        "min_inputs": 2,
        "required": ["keys", "bring"],
        "optional": [],
        "notes": (
            "Takes EXACTLY TWO inputs: inputs[0] is the SUBJECT, inputs[1] is the REFERENCE. "
            "Row count and order come out unchanged, because the reference is required to hold "
            "at most ONE row per key: the runtime asks pandas to VERIFY that, so a reference "
            "that repeats a key FAILS THE RUN rather than silently multiplying rows. Use "
            "`expand` when the fan-out is intended. Every subject row survives — an unmatched "
            "one carries nulls for the brought columns — and an unmatched reference row is "
            "dropped. This stage NEVER drops a subject row: to drop rows (e.g. inner-join "
            "semantics), follow it with a `filter_rows` on a brought column being non-null, "
            "which records the row loss instead of hiding it. "
            "The output is every subject column plus exactly `bring`: each entry must name a "
            "reference column the subject does not already carry — a collision is refused, "
            "never renamed, so rename the reference column upstream if both must survive. A "
            "key pair with the SAME name on both sides needs no bring entry; the subject's own "
            "key column already carries the matched value. output_schema may name only columns "
            "the join produces — anything else is rejected when the stage is saved."
        ),
    },
    "expand": {
        "summary": "Joins brought reference columns into each subject row, fanning one subject row out to several (many-to-many).",
        "blocks": ["join"],
        "requires_inputs": True,
        "min_inputs": 2,
        "required": ["keys", "bring"],
        "optional": [],
        "notes": (
            "Takes EXACTLY TWO inputs: inputs[0] is the SUBJECT, inputs[1] is the REFERENCE. "
            "The reference MAY hold several rows per key, so one subject row may come out as "
            "several — deliberate fan-out. Use `enrich` instead when the reference is meant to "
            "be unique on the key and a repeat is a bug you want caught. Every subject row "
            "survives — an unmatched one carries nulls for the brought columns — and an "
            "unmatched reference row is dropped. This stage NEVER drops a subject row: to drop "
            "rows (e.g. inner-join semantics), follow it with a `filter_rows` on a brought "
            "column being non-null, which records the row loss instead of hiding it. "
            "The output is every subject column plus exactly `bring`: each entry must name a "
            "reference column the subject does not already carry — a collision is refused, "
            "never renamed, so rename the reference column upstream if both must survive. A "
            "key pair with the SAME name on both sides needs no bring entry; the subject's own "
            "key column already carries the matched value. output_schema may name only columns "
            "the join produces — anything else is rejected when the stage is saved."
        ),
    },
}

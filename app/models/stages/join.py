"""enrich/expand stage: the join handle config and its column checks."""
from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Literal, Optional

from pydantic import Field, model_validator

from app.models.schema import StageConfig, _Base
from app.models.stage_base import StageBase, StageInput, StageType
from app.models.stages.shared import (
    COLUMN_ISSUE,
    INTERNAL_COLUMN_PREFIX,
    find_declared_vs_computed_issues,
    resolve_input_columns,
)
from app.models.stages.node_spec import NodeTypeSpec
from app.models.stages.signature import ExtendsSignature

if TYPE_CHECKING:
    from app.models.schema import TableSchema


class JoinKey(_Base):
    left: str
    right: str


class JoinConfig(StageConfig):
    """enrich/expand handle. Cardinality lives in the stage TYPE, not here."""
    FINGERPRINT_FIELDS: ClassVar[frozenset[str]] = frozenset({"keys", "enrich_with"})
    INCIDENTAL_FIELDS: ClassVar[frozenset[str]] = frozenset()

    keys: list[JoinKey] = Field(min_length=1)
    enrich_with: dict[str, str] = Field(
        min_length=1,
        description=(
            "Reference column -> the name it lands under, usually the same "
            "(`region: region`). A join only ADDS: a landed name the subject "
            "already carries is refused — pick a new one (`score: score_r`). "
            "Null on an unmatched row."
        ),
    )

    @model_validator(mode="after")
    def _landed_names_well_formed(self) -> "JoinConfig":
        seen: set[str] = set()
        for landed in self.enrich_with.values():
            if landed in seen:
                raise ValueError(f"join.enrich_with lands two columns as {landed!r}")
            if landed.startswith(INTERNAL_COLUMN_PREFIX):
                raise ValueError(
                    f"join.enrich_with lands {landed!r} inside the reserved "
                    f"`{INTERNAL_COLUMN_PREFIX}` namespace"
                )
            seen.add(landed)
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


ENRICH_WITH_REWRITE_ISSUE = (
    "stage '{sid}': join.enrich_with lands '{landed}' on the subject input "
    "'{subject}', which already carries it — a join only ever ADDS; land the "
    "source under a name the subject does not carry (`{src}: {src}_r`) or "
    "drop the entry"
)
ENRICH_WITH_SHADOWS_KEY_ISSUE = (
    "stage '{sid}': join.enrich_with lands '{src}' as '{landed}', but '{landed}' is "
    "a join key on the reference side and the merge reads that column — land "
    "it under a different name"
)


def find_join_column_issues(stage: "JoinStage") -> list[str]:
    """Keys and enrich_with sources their side's edge cannot satisfy; a landed name the subject carries."""
    join = stage.join
    left = resolve_input_columns(stage, 0)
    right = resolve_input_columns(stage, 1)
    right_keys = {key.right for key in join.keys}
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
    for src, landed in join.enrich_with.items():
        if src not in right:
            issues.append(
                COLUMN_ISSUE.format(sid=stage.id, field="join.enrich_with", col=src, cols=sorted(right))
            )
        if landed in left:
            issues.append(ENRICH_WITH_REWRITE_ISSUE.format(
                sid=stage.id, landed=landed, subject=stage.inputs[0].id, src=src
            ))
        elif landed in right_keys and landed != src:
            issues.append(ENRICH_WITH_SHADOWS_KEY_ISSUE.format(
                sid=stage.id, src=src, landed=landed
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
    """Keys must be read from their side, adds must be exactly `enrich_with`; rewrites are refused."""
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

    src_by_landed = {landed: src for src, landed in stage.join.enrich_with.items()}
    reference_types = {c.name: c.type for c in reference.table_schema.columns}
    for column in signature.adds:
        src = src_by_landed.get(column.name)
        if src is None:
            issues.append(
                f"stage '{stage.id}': signature adds `{column.name}`, which "
                f"join.enrich_with does not land (landed: {sorted(src_by_landed)})"
            )
        elif reference_types.get(src, column.type) != column.type:
            issues.append(
                f"stage '{stage.id}': signature adds `{column.name}` as "
                f"{column.type!r} but its source `{src}` supplies "
                f"{reference_types[src]!r}"
            )
    added = {column.name for column in signature.adds}
    issues.extend(
        f"stage '{stage.id}': join.enrich_with lands `{landed}` but the signature "
        f"does not add it"
        for landed in src_by_landed
        if landed not in added
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
    """Left columns, then each `enrich_with` entry under its landed name with its source's type."""
    right_types = {c.name: c.type for c in right.columns}
    joined: dict[str, str] = {c.name: c.type for c in left.columns}
    for src, landed in join.enrich_with.items():
        if src in right_types and landed not in joined:
            joined[landed] = right_types[src]
    return joined

# Authoring copy for this module's stage type(s); assembled into NODE_TYPES.
NODE_TYPE_SPECS: dict[str, NodeTypeSpec] = {
    "enrich": NodeTypeSpec(
        summary="Adds brought reference columns to each subject row; the reference must be unique on the key (many-to-one).",
        signature_form="extends",
        blocks=["join"],
        requires_inputs=True,
        min_inputs=2,
        required=["keys", "enrich_with"],
        optional=[],
        notes=(
            "Takes EXACTLY TWO inputs: inputs[0] is the SUBJECT, inputs[1] is the REFERENCE. "
            "Row count and order come out unchanged: the runtime VERIFIES the reference is "
            "unique on the key, and a repeat FAILS THE RUN — use `expand` for intended "
            "fan-out. Every subject row survives (unmatched rows carry nulls in the landed "
            "columns); dropping rows is `filter_rows`' job. `enrich_with` maps each reference "
            "column to the name it lands under, usually the same. A join only ADDS: a landed "
            "name the subject already carries is refused — pick a new one (`score: score_r`). "
            "A same-named key pair needs no entry. output_schema may name only columns the "
            "join produces."
        ),
    ),
    "expand": NodeTypeSpec(
        summary="Joins brought reference columns into each subject row, fanning one subject row out to several (many-to-many).",
        signature_form="extends",
        blocks=["join"],
        requires_inputs=True,
        min_inputs=2,
        required=["keys", "enrich_with"],
        optional=[],
        notes=(
            "Takes EXACTLY TWO inputs: inputs[0] is the SUBJECT, inputs[1] is the REFERENCE. "
            "The reference MAY repeat a key, so one subject row can fan out to several — use "
            "`enrich` when a repeat is a bug you want caught. Every subject row survives "
            "(unmatched rows carry nulls in the landed columns); dropping rows is "
            "`filter_rows`' job. `enrich_with` maps each reference column to the name it "
            "lands under, usually the same. A join only ADDS: a landed name the subject "
            "already carries is refused — pick a new one (`score: score_r`). A same-named "
            "key pair needs no entry. output_schema may name only columns the join produces."
        ),
    ),
}

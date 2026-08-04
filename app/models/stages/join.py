"""enrich/expand stage: the shared join handle config plus column validation —
keys must resolve against their side's edge, every `bring` source must exist on
the reference with its landed name new to the subject (a join adds, never
rewrites), and a declared output_schema must be deliverable as the subject's
columns plus the landed ones."""
from __future__ import annotations

from typing import Any, TYPE_CHECKING, ClassVar, Literal, Optional

from pydantic import Field, model_validator

from app.models.schema import StageConfig, _Base
from app.models.stage_base import StageBase, StageInput, StageType
from app.models.stages.shared import (
    COLUMN_ISSUE,
    INTERNAL_COLUMN_PREFIX,
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
    landed name. A join only ever ADDS — a landed name the subject already
    carries would rewrite that column, so it is refused; the out is landing
    the same source under a name the subject does not carry (`score:
    score_r`). A key pair with the SAME name on both sides needs no bring
    entry: the subject's own key column already carries the matched value."""
    # Every field changes what this stage computes (keys, brought columns) —
    # see Stage.compute_definition_fingerprint.
    FINGERPRINT_FIELDS: ClassVar[frozenset[str]] = frozenset({"keys", "bring"})
    INCIDENTAL_FIELDS: ClassVar[frozenset[str]] = frozenset()

    keys: list[JoinKey] = Field(min_length=1)
    bring: dict[str, str] = Field(
        min_length=1,
        description=(
            "Reference column -> the name it lands under in the output, "
            "usually the same (`region: region`). Every source must exist on "
            "the reference input; every landed name must be absent from the "
            "subject, because a join only ever adds — to bring a column the "
            "subject also carries, land it under a new name (`score: "
            "score_r`). On an unmatched subject row every brought column is "
            "null."
        ),
    )

    @model_validator(mode="after")
    def _landed_names_well_formed(self) -> "JoinConfig":
        seen: set[str] = set()
        for landed in self.bring.values():
            if landed in seen:
                raise ValueError(f"join.bring lands two columns as {landed!r}")
            if landed.startswith(INTERNAL_COLUMN_PREFIX):
                raise ValueError(
                    f"join.bring lands {landed!r} inside the reserved "
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


BRING_REWRITE_ISSUE = (
    "stage '{sid}': join.bring lands '{landed}' on the subject input "
    "'{subject}', which already carries it — a join only ever ADDS; land the "
    "source under a name the subject does not carry (`{src}: {src}_r`) or "
    "drop the entry"
)
BRING_SHADOWS_KEY_ISSUE = (
    "stage '{sid}': join.bring lands '{src}' as '{landed}', but '{landed}' is "
    "a join key on the reference side and the merge reads that column — land "
    "it under a different name"
)


def find_join_column_issues(stage: "JoinStage") -> list[str]:
    """Keys and bring sources their side's edge cannot satisfy; a landed name the subject carries."""
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
    for src, landed in join.bring.items():
        if src not in right:
            issues.append(
                COLUMN_ISSUE.format(sid=stage.id, field="join.bring", col=src, cols=sorted(right))
            )
        if landed in left:
            issues.append(BRING_REWRITE_ISSUE.format(
                sid=stage.id, landed=landed, subject=stage.inputs[0].id, src=src
            ))
        elif landed in right_keys and landed != src:
            issues.append(BRING_SHADOWS_KEY_ISSUE.format(
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

    src_by_landed = {landed: src for src, landed in stage.join.bring.items()}
    reference_types = {c.name: c.type for c in reference.table_schema.columns}
    for column in signature.adds:
        src = src_by_landed.get(column.name)
        if src is None:
            issues.append(
                f"stage '{stage.id}': signature adds `{column.name}`, which "
                f"join.bring does not land (landed: {sorted(src_by_landed)})"
            )
        elif reference_types.get(src, column.type) != column.type:
            issues.append(
                f"stage '{stage.id}': signature adds `{column.name}` as "
                f"{column.type!r} but its source `{src}` supplies "
                f"{reference_types[src]!r}"
            )
    added = {column.name for column in signature.adds}
    issues.extend(
        f"stage '{stage.id}': join.bring lands `{landed}` but the signature "
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
    """The columns the join handle emits, each mapped to its type: every left
    column under its own name and type, then each `bring` entry under its
    landed name with its SOURCE right column's type. An entry whose source the
    right edge does not supply, or whose landed name the left side already
    does, contributes nothing here — `find_join_column_issues` reports it."""
    right_types = {c.name: c.type for c in right.columns}
    joined: dict[str, str] = {c.name: c.type for c in left.columns}
    for src, landed in join.bring.items():
        if src in right_types and landed not in joined:
            joined[landed] = right_types[src]
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
            "The output is every subject column plus exactly `bring` — a map of reference "
            "column to the name it lands under, usually the same (`region: region`). A join "
            "only ever ADDS: a landed name the subject already carries would rewrite that "
            "column and is refused; to bring a column the subject also has, land it under a "
            "new name (`score: score_r`). A key pair with the SAME name on both sides needs "
            "no bring entry; the subject's own key column already carries the matched value. "
            "output_schema may name only columns the join produces — anything else is "
            "rejected when the stage is saved."
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
            "The output is every subject column plus exactly `bring` — a map of reference "
            "column to the name it lands under, usually the same (`region: region`). A join "
            "only ever ADDS: a landed name the subject already carries would rewrite that "
            "column and is refused; to bring a column the subject also has, land it under a "
            "new name (`score: score_r`). A key pair with the SAME name on both sides needs "
            "no bring entry; the subject's own key column already carries the matched value. "
            "output_schema may name only columns the join produces — anything else is "
            "rejected when the stage is saved."
        ),
    },
}

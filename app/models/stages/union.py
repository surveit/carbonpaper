"""union stage: handle config, plus column validation — every declared input
must share an identical schema (prose aside), and a declared output_schema
must equal that shared schema."""
from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from app.models.schema import _Base

if TYPE_CHECKING:
    from app.models.stage import Stage


class UnionConfig(_Base):
    """union handle. No fields: a union's behavior is fixed entirely by its
    (schema-identical) declared inputs, concatenated in declared order."""
    FINGERPRINT_FIELDS: ClassVar[frozenset[str]] = frozenset()
    INCIDENTAL_FIELDS: ClassVar[frozenset[str]] = frozenset()


def find_union_column_issues(stage: "Stage") -> list[str]:
    """One issue per input (after the first) whose declared schema disagrees
    with the first input's, naming the differing columns."""
    schemas = [(ref.id, ref.table_schema) for ref in stage.inputs]
    reference_id, reference = schemas[0]
    issues: list[str] = []
    for input_id, schema in schemas[1:]:
        differing = sorted(schema.differing_column_names(reference))
        if differing:
            issues.append(
                f"stage '{stage.id}': union input '{input_id}' disagrees with input "
                f"'{reference_id}' on column(s) {differing}"
            )
    return issues


def find_union_output_issues(stage: "Stage") -> list[str]:
    """Issue naming any column where the declared output_schema disagrees with
    the union's (already schema-identical) inputs."""
    assert stage.output_schema is not None  # Stage._schemas_declared guarantees this off publish
    reference = stage.inputs[0].table_schema
    differing = sorted(stage.output_schema.differing_column_names(reference))
    if not differing:
        return []
    return [
        f"stage '{stage.id}': output_schema disagrees with the union's shared "
        f"input schema on column(s) {differing}"
    ]

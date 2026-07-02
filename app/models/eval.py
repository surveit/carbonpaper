"""Eval as a separate overlay — NOT part of the DAG.

Eval consumes generation; generation has no knowledge of eval (the runner needs
none). An EvalSpec names the generation schema it grades (`evaluates`) and the
columns it mirrors; `build_ground_truth_schema` DERIVES the ground-truth schema
FROM that generation schema, so ground truth stays consistent with what the
pipeline produces by construction — it can't silently drift.
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import Field, ValidationError, model_validator

from app.models.named_schemas import NamedColumn, NamedSchema, SchemaKind
from app.models.schema import SourceRef, _Base, format_errors


class EvalSpec(_Base):
    """Grades one generation schema against external ground truth."""
    name: str
    evaluates: str                                  # the generation schema this grades
    metrics: list[str]
    mirror_columns: Optional[list[str]] = None      # generation columns the truth mirrors
    join_on: Optional[list[str]] = None
    extra_columns: list[NamedColumn] = Field(default_factory=list)
    source: Optional[SourceRef] = None              # where the ground truth comes from
    notes: Optional[str] = None

    @model_validator(mode="after")
    def _has_metrics(self) -> "EvalSpec":
        if not self.metrics:
            raise ValueError(f"eval `{self.name}`: declares no metrics")
        return self


def build_ground_truth_schema(eval_spec: EvalSpec, gen: NamedSchema) -> NamedSchema:
    """Derive the ground-truth schema FROM the generation schema it grades.
    Mirrored columns ARE the generation columns (consistent by construction);
    eval-only `extra_columns` are appended. The PK carries over only if all of its
    columns are mirrored. Title is inherited from the generation schema."""
    gen_cols = {c.name: c for c in gen.columns}
    mirror = eval_spec.mirror_columns or list(gen_cols)
    columns: list[NamedColumn] = [gen_cols[n] for n in mirror if n in gen_cols]
    columns += list(eval_spec.extra_columns)
    pk = [k for k in (gen.primary_key or []) if k in mirror] or None
    # An exclusive arc carries over only if every column in it is mirrored, so the
    # ground truth is bound by the same XOR constraint as what it grades.
    arcs = [a for a in (gen.exclusive_arcs or []) if all(c in mirror for c in a)] or None
    return NamedSchema(
        name=eval_spec.name,
        kind=SchemaKind.ground_truth,
        title=gen.title,
        columns=columns,
        primary_key=pk,
        exclusive_arcs=arcs,
        source=eval_spec.source,
        notes=eval_spec.notes,
    )


def validate_eval_spec(eval_spec: dict[str, Any],
                       gen_by_name: dict[str, dict[str, Any]]) -> list[str]:
    """Non-fatal: is this eval spec consistent with the generation data model?
    It must grade a real generation schema, mirror/join on real columns of it, and
    add no eval-only column that collides with a generation column."""
    try:
        spec = EvalSpec.model_validate(eval_spec)
    except ValidationError as err:
        return format_errors(err)
    gen = gen_by_name.get(spec.evaluates)
    if gen is None:
        return [f"eval `{spec.name}`: evaluates unknown generation schema `{spec.evaluates}`"]
    gen_cols = {c.get("name") for c in gen.get("columns") or []}
    issues: list[str] = []
    for key, vals in (("mirror_columns", spec.mirror_columns), ("join_on", spec.join_on)):
        for col in vals or []:
            if col not in gen_cols:
                issues.append(f"eval `{spec.name}`.{key}: `{col}` is not a column of `{spec.evaluates}`")
    for col in sorted({c.name for c in spec.extra_columns} & gen_cols):
        issues.append(f"eval `{spec.name}`: extra column `{col}` collides with generation `{spec.evaluates}`")
    return issues

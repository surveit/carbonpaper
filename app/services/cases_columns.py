"""Derive the cases-file schema an eval's checks require: the override
stage's whole output (injected inputs) plus one answer column per check,
named after the target column it grades. Single source of truth for this
derivation -- called by the schema preview / `cases-schema` endpoint, the
authoring form's saved `TableRef.table_schema`, and
`app.services.eval_compat.check_eval_compatibility`'s override-coverage
check, so all three always agree on the exact column names a cases file must
carry.

Name clash: a check's target column can share a name with one of the
override stage's own output columns. A flat table can't hold two columns
with the same name, so a clashing name is disambiguated: the injected input
is written `override.<name>`, the answer is written `output.<name>`, and a
warning names the clash so the caller can surface it. Non-clashing names are
left as-is.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.models.schema import Column
from app.models.stage import Stage


@dataclass
class CasesColumns:
    """`injected` and `answers` never share a column name -- clashing names
    are prefixed apart (`override.` / `output.`) before either list is built
    -- so concatenating them is always duplicate-free.

    `problems` is split by what it's about so a caller that already reports
    override/target existence and per-check target-column checks elsewhere
    (`check_eval_compatibility`'s own conditions) can take only the piece it
    doesn't already cover, instead of re-surfacing the same fact twice under
    different wording."""
    injected: list[Column] = field(default_factory=list)
    answers: list[Column] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    override_problems: list[str] = field(default_factory=list)
    target_problems: list[str] = field(default_factory=list)
    check_problems: list[str] = field(default_factory=list)

    @property
    def columns(self) -> list[Column]:
        return [*self.injected, *self.answers]

    @property
    def problems(self) -> list[str]:
        return [*self.override_problems, *self.target_problems, *self.check_problems]


def derive_cases_columns(
    override: Stage | None,
    target: Stage | None,
    check_actuals: list[str],
) -> CasesColumns:
    """`override` / `target` are the stages an eval's `override_stage` /
    `target_stage` id resolved to (`None` if the id names no stage in the
    methodology). `check_actuals` are each check's `actual` target column;
    blanks (an unfilled row in the authoring form) are ignored."""
    override_problems: list[str] = []
    target_problems: list[str] = []
    check_problems: list[str] = []

    override_names: set[str] = set()
    if override is None:
        override_problems.append("override stage does not exist in the methodology")
    elif override.output_schema is None:
        override_problems.append(f"override stage `{override.id}` declares no output schema")
    else:
        override_names = {c.name for c in override.output_schema.columns}

    target_types: dict[str, str] = {}
    if target is None:
        target_problems.append("target stage does not exist in the methodology")
    elif target.output_schema is None:
        target_problems.append(f"target stage `{target.id}` declares no output schema")
    else:
        target_types = {c.name: c.type for c in target.output_schema.columns}

    actuals = [a for a in check_actuals if a]
    clash = {a for a in actuals if a in override_names}

    injected: dict[str, Column] = {}
    if override is not None and override.output_schema is not None:
        for col in override.output_schema.columns:
            file_name = f"override.{col.name}" if col.name in clash else col.name
            injected[file_name] = Column(name=file_name, type=col.type)

    answers: dict[str, Column] = {}
    for actual in actuals:
        col_type = target_types.get(actual)
        if col_type is None:
            target_label = f"`{target.id}`" if target is not None else "the target"
            check_problems.append(
                f"check asserts on `{actual}`, which target {target_label} does not emit"
            )
            continue
        file_name = f"output.{actual}" if actual in clash else actual
        answers[file_name] = Column(name=file_name, type=col_type)

    warnings = [
        f"`{name}` is both an injected input and a checked output -- the "
        f"cases file names them `override.{name}` (input) and `output.{name}` (answer)."
        for name in sorted(clash)
    ]

    return CasesColumns(
        injected=list(injected.values()), answers=list(answers.values()),
        warnings=warnings, override_problems=override_problems,
        target_problems=target_problems, check_problems=check_problems,
    )


__all__ = ["CasesColumns", "derive_cases_columns"]

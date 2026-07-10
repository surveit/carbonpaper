"""Derive the cases-file schema an eval's checks require: the override
stage's whole output (injected inputs) plus one answer column per check,
named after the target column it grades. Single source of truth for this
derivation -- called by the schema preview / `cases-schema` endpoint, the
authoring form's saved `TableRef.table_schema`, and
`app.services.eval_compatibility.check_eval_compatibility`'s override-coverage
check, so all three always agree on the exact column names a cases file must
carry.

Each derived column is a copy of the source stage's own `Column` (only the
`name` is overridden) -- `type`, `nullable`, `range`, `description`, and
`source` all carry through unchanged, so a caller can render or validate the
full declared shape instead of just a name and type.

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

    `override_problems` is split out from `other_problems` because
    `check_eval_compatibility` already reports target-existence and
    per-check target-column problems itself (in its own wording) and would
    otherwise double-report them; it takes only `override_problems`, the one
    fact it doesn't already cover. `problems` gives a caller that has no such
    overlap (e.g. a standalone schema-preview endpoint) everything at once."""
    injected: list[Column] = field(default_factory=list)
    answers: list[Column] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    override_problems: list[str] = field(default_factory=list)
    other_problems: list[str] = field(default_factory=list)

    @property
    def columns(self) -> list[Column]:
        return [*self.injected, *self.answers]

    @property
    def problems(self) -> list[str]:
        return [*self.override_problems, *self.other_problems]


def derive_cases_columns(
    override: Stage | None,
    target: Stage | None,
    check_actuals: list[str],
) -> CasesColumns:
    """`override` / `target` are the stages an eval's `override_stage` /
    `target_stage` id resolved to (`None` if the id names no stage in the
    workflow). `check_actuals` are each check's `actual` target column;
    blanks (an unfilled row in the authoring form) are ignored."""
    override_columns, override_problems = _override_columns(override)
    target_columns, target_problems = _target_columns(target)

    actuals = [a for a in check_actuals if a]
    clash = _clash_names(override_columns, actuals)

    injected = _rename_clashing(override_columns, clash, prefix="override.")
    answers, check_problems = _answer_columns(target_columns, actuals, clash, target)

    return CasesColumns(
        injected=injected, answers=answers,
        warnings=_clash_warnings(clash),
        override_problems=override_problems,
        other_problems=target_problems + check_problems,
    )


# ── Per-side column resolution ────────────────────────────────────────────────
def _override_columns(override: Stage | None) -> tuple[list[Column], list[str]]:
    """The override stage's declared output columns, or `[]` plus a problem
    naming why (no such stage, or the stage declares no output schema)."""
    if override is None:
        return [], ["override stage does not exist in the workflow"]
    if override.output_schema is None:
        return [], [f"override stage `{override.id}` declares no output schema"]
    return list(override.output_schema.columns), []


def _target_columns(target: Stage | None) -> tuple[dict[str, Column], list[str]]:
    """The target stage's declared output columns by name, or `{}` plus a
    problem naming why (no such stage, or the stage declares no output
    schema)."""
    if target is None:
        return {}, ["target stage does not exist in the workflow"]
    if target.output_schema is None:
        return {}, [f"target stage `{target.id}` declares no output schema"]
    return {c.name: c for c in target.output_schema.columns}, []


# ── Name-clash handling ─────────────────────────────────────────────────────
def _clash_names(override_columns: list[Column], actuals: list[str]) -> set[str]:
    override_names = {c.name for c in override_columns}
    return {a for a in actuals if a in override_names}


def _rename_clashing(columns: list[Column], clash: set[str], *, prefix: str) -> list[Column]:
    """A copy of `columns`, each clashing name rewritten as `{prefix}{name}`."""
    renamed: dict[str, Column] = {}
    for col in columns:
        name = f"{prefix}{col.name}" if col.name in clash else col.name
        renamed[name] = col.model_copy(update={"name": name})
    return list(renamed.values())


def _answer_columns(target_columns: dict[str, Column], actuals: list[str],
                    clash: set[str], target: Stage | None) -> tuple[list[Column], list[str]]:
    """One answer column per `actual` that resolves against `target_columns`
    (clash-renamed `output.<name>`); an `actual` with no matching target
    column is skipped and reported instead."""
    answers: dict[str, Column] = {}
    problems: list[str] = []
    for actual in actuals:
        target_col = target_columns.get(actual)
        if target_col is None:
            label = f"`{target.id}`" if target is not None else "the target"
            problems.append(f"check asserts on `{actual}`, which target {label} does not emit")
            continue
        name = f"output.{actual}" if actual in clash else actual
        answers[name] = target_col.model_copy(update={"name": name})
    return list(answers.values()), problems


def _clash_warnings(clash: set[str]) -> list[str]:
    return [
        f"`{name}` is both an injected input and a checked output -- the "
        f"cases file names them `override.{name}` (input) and `output.{name}` (answer)."
        for name in sorted(clash)
    ]

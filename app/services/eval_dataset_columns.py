"""Derive the eval-dataset schema an eval's checks require: the override
stage's whole output (injected inputs) plus one expected-output column per
check, named after the target column it grades. Single source of truth for
this derivation -- called by
`app.services.eval_compatibility.check_eval_compatibility`'s override-coverage
check, so callers always agree on the exact column names an eval-dataset
file must carry.

Each derived column is a copy of the source stage's own `Column` (only the
`name` is overridden) -- `type`, `nullable`, `range`, `description`, and
`source` all carry through unchanged, so a caller can render or validate the
full declared shape instead of just a name and type.

Name conflict: a check's target column can share a name with one of the
override stage's own output columns. A flat table can't hold two columns
with the same name, so a conflicting name is deconflicted: the injected
input is written `override.<name>`, the expected-output column is written
`output.<name>`. Non-conflicting names are left as-is. (Surfacing that
conflict as a warning to an author is a UI/preview concern for the authoring
form, not this derivation.)
"""
from __future__ import annotations

from app.models.schema import Column, TableSchema
from app.models.stage import Stage


def get_output_columns_from_stage(stage: Stage) -> list[Column]:
    """The output columns `stage` declares, or `[]` if it declares no output
    schema."""
    if stage.output_schema is None:
        return []
    return list(stage.output_schema.columns)


def derive_eval_dataset_columns(
    override: Stage, target: Stage, check_output_columns: list[str],
) -> TableSchema:
    """The eval-dataset schema: `override`'s declared output columns
    (injected as that stage's whole output) plus one expected-output column
    per name in `check_output_columns` that resolves against `target`'s
    declared output. A name that resolves to no target column is skipped --
    `check_eval_compatibility` reports that separately, against the target's
    declared output directly."""
    injected, expected_output = _deconflicted_columns(override, target, check_output_columns)
    return TableSchema(columns=[*injected, *expected_output])


def get_injected_columns(
    override: Stage, target: Stage, check_output_columns: list[str],
) -> list[Column]:
    """The override-stage columns of the eval-dataset schema
    `derive_eval_dataset_columns` would build for this override/target/check
    set, deconflicted against the checks' expected-output columns -- what a
    caller needs to check that an eval-dataset file covers `override`'s
    output specifically."""
    injected, _ = _deconflicted_columns(override, target, check_output_columns)
    return injected


def _deconflicted_columns(
    override: Stage, target: Stage, check_output_columns: list[str],
) -> tuple[list[Column], list[Column]]:
    override_columns = get_output_columns_from_stage(override)
    target_by_name = {c.name: c for c in get_output_columns_from_stage(target)}
    expected_output_columns = [
        target_by_name[name]
        for name in dict.fromkeys(n for n in check_output_columns if n)
        if name in target_by_name
    ]
    return deconflict_column_names(override_columns, expected_output_columns)


# ── Name-conflict handling ──────────────────────────────────────────────────
def deconflict_column_names(
    override_columns: list[Column], expected_output_columns: list[Column],
) -> tuple[list[Column], list[Column]]:
    """`override_columns` and `expected_output_columns`, renamed so no name is
    shared between them: a name present on both sides is rewritten
    `override.<name>` on the injected side and `output.<name>` on the
    expected-output side."""
    conflicts = _check_for_column_name_conflicts(override_columns, expected_output_columns)
    injected = _rename_columns(override_columns, conflicts, prefix="override.")
    expected_output = _rename_columns(expected_output_columns, conflicts, prefix="output.")
    return injected, expected_output


def _check_for_column_name_conflicts(
    override_columns: list[Column], expected_output_columns: list[Column],
) -> set[str]:
    override_names = {c.name for c in override_columns}
    return {c.name for c in expected_output_columns if c.name in override_names}


def _rename_columns(columns: list[Column], conflicts: set[str], *, prefix: str) -> list[Column]:
    """A copy of `columns`, each conflicting name rewritten as `{prefix}{name}`."""
    renamed: dict[str, Column] = {}
    for col in columns:
        name = f"{prefix}{col.name}" if col.name in conflicts else col.name
        renamed[name] = col.model_copy(update={"name": name})
    return list(renamed.values())

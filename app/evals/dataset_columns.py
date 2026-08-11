"""The override-stage columns an eval-dataset file must inject: the override
stage's whole output, deconflicted against the checks' expected-output names.
A check's target column may share a name with an override output column; a flat
table can't hold both, so the injected input becomes `override.<name>` and the
expected output `output.<name>`. Missing preconditions raise ValueError.
"""
from __future__ import annotations

from app.models.schema import Column
from app.models.stage import Stage


def get_output_columns_from_stage(stage: Stage) -> list[Column]:
    output_schema = stage.resolve_output_schema()
    if output_schema is None:
        raise ValueError(f"stage {stage.id!r} declares no output schema")
    return list(output_schema.columns)


def get_injected_columns(
    override: Stage, target: Stage, check_output_columns: list[str],
) -> list[Column]:
    injected, _ = _deconflicted_columns(override, target, check_output_columns)
    return injected


def _deconflicted_columns(
    override: Stage, target: Stage, check_output_columns: list[str],
) -> tuple[list[Column], list[Column]]:
    override_columns = get_output_columns_from_stage(override)
    target_by_name = {c.name: c for c in get_output_columns_from_stage(target)}
    expected_output_columns = []
    for name in dict.fromkeys(n for n in check_output_columns if n):
        if name not in target_by_name:
            raise ValueError(f"target stage {target.id!r} does not emit checked column {name!r}")
        expected_output_columns.append(target_by_name[name])
    return deconflict_column_names(override_columns, expected_output_columns)


# ── Name-conflict handling ──────────────────────────────────────────────────
def deconflict_column_names(
    override_columns: list[Column], expected_output_columns: list[Column],
) -> tuple[list[Column], list[Column]]:
    conflicts = _find_column_name_conflicts(override_columns, expected_output_columns)
    injected = _rename_columns(override_columns, conflicts, prefix="override.")
    expected_output = _rename_columns(expected_output_columns, conflicts, prefix="output.")
    return injected, expected_output


def _find_column_name_conflicts(
    override_columns: list[Column], expected_output_columns: list[Column],
) -> set[str]:
    override_names = {c.name for c in override_columns}
    return {c.name for c in expected_output_columns if c.name in override_names}


def _rename_columns(columns: list[Column], conflicts: set[str], *, prefix: str) -> list[Column]:
    renamed: dict[str, Column] = {}
    for col in columns:
        name = f"{prefix}{col.name}" if col.name in conflicts else col.name
        renamed[name] = col.model_copy(update={"name": name})
    return list(renamed.values())

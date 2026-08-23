"""Architecture: `app.core.stage_cache` is the only channel a stage handler may
use to persist something outliving its own run. Two rules: no `"project_dir"`
dict key under `app/runtime/stages`, and no `.save()`/`.delete()` under
`app/runtime` except in a module that DEFINES a PersistenceScope.RUN record —
the run's OWN state, which is what `run_dir` writes were before it moved here.
"""
from __future__ import annotations

import ast
from pathlib import Path

from arch import check_no_dict_keys
from arch._helpers import (
    collect_called_methods,
    find_class_body_assignment,
    find_subclasses_of,
    parse_module,
)
from arch.scope import find_source_files_under

_RUNTIME_STAGES_DIR = Path(__file__).resolve().parents[1] / "stages"
_RUNTIME_DIR = Path(__file__).resolve().parents[1]

_BANNED_PERSISTENCE_METHODS = frozenset({"save", "delete"})

# Written as a property of the module rather than a list of filenames, so it stays
# the rule it means — project-scope writes from the runtime are still the bug this
# catches — and needs no allowlist edit for the next run-scoped record.
_RUN_SCOPE = "PersistenceScope.RUN"
_RECORDS_MODULE = "app.models.records"
_RECORDS_DIR = Path(__file__).resolve().parents[2] / "models" / "records"


def defines_a_run_scoped_record(tree: ast.Module) -> bool:
    """Whether `tree` declares a class with `SCOPE: ... = PersistenceScope.RUN`."""
    return any(
        isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "SCOPE"
        and node.value is not None
        and ast.unparse(node.value) == _RUN_SCOPE
        for node in ast.walk(tree)
    )


def read_record_scopes() -> dict[str, str]:
    """Every record declared under app/models/records, by the scope it carries."""
    scopes: dict[str, str] = {}
    for path in find_source_files_under(_RECORDS_DIR):
        tree = parse_module(path)
        for node in find_subclasses_of(tree, "PersistedModel"):
            scope = find_class_body_assignment(node, "SCOPE")
            value = getattr(scope, "value", None)
            scopes[node.name] = ast.unparse(value) if value is not None else ""
    return scopes


def find_imported_record_names(tree: ast.Module) -> set[str]:
    return {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module is not None
        and node.module.startswith(_RECORDS_MODULE)
        for alias in node.names
    }


def saves_only_run_scoped_records(tree: ast.Module, scopes: dict[str, str]) -> bool:
    """A record declared elsewhere is still the run's own state if its scope says so."""
    imported = find_imported_record_names(tree) & set(scopes)
    return bool(imported) and all(scopes[name] == _RUN_SCOPE for name in imported)


def find_persisted_write_call_offenders(paths: list[Path]) -> list[str]:
    scopes = read_record_scopes()
    offenders: list[str] = []
    for path in paths:
        tree = parse_module(path)
        if defines_a_run_scoped_record(tree) or saves_only_run_scoped_records(tree, scopes):
            continue
        hits = collect_called_methods(tree) & _BANNED_PERSISTENCE_METHODS
        if hits:
            offenders.append(f"{path.name}: {sorted(hits)}")
    return offenders


def test_stage_modules_never_hold_a_project_dir_key() -> None:
    offenders = check_no_dict_keys(find_source_files_under(_RUNTIME_STAGES_DIR), {"project_dir"})
    assert not offenders, (
        "a stage handler must reach project scope only through RunContext's own "
        "fields (identity, stage_cache) — never a `\"project_dir\"` dict key "
        "stashed on the side:\n  " + "\n  ".join(offenders)
    )


def test_runtime_never_calls_save_or_delete_directly() -> None:
    offenders = find_persisted_write_call_offenders(find_source_files_under(_RUNTIME_DIR))
    assert not offenders, (
        "app/runtime must reach a cross-run write only through the cache seam's "
        "own StageCache.put — never PersistedModel.save()/delete() directly, "
        "even when it holds a legitimately-read entry instance:\n  "
        + "\n  ".join(offenders)
    )


# --- unit tests for find_persisted_write_call_offenders, on inline snippets ---


def test_find_persisted_write_call_offenders_flags_a_save_call(tmp_path: Path) -> None:
    target = tmp_path / "m.py"
    target.write_text("entry.save()\n", encoding="utf-8")
    assert find_persisted_write_call_offenders([target]) == ["m.py: ['save']"]


def test_find_persisted_write_call_offenders_flags_a_delete_call(tmp_path: Path) -> None:
    target = tmp_path / "m.py"
    target.write_text("Entry.delete(entry_id)\n", encoding="utf-8")
    assert find_persisted_write_call_offenders([target]) == ["m.py: ['delete']"]


def test_find_persisted_write_call_offenders_ignores_read_only_calls(tmp_path: Path) -> None:
    target = tmp_path / "m.py"
    target.write_text(
        "cache.get(project, sid, fp, ifp)\ncache.find_entries(project, sid, fp)\n",
        encoding="utf-8",
    )
    assert find_persisted_write_call_offenders([target]) == []


def test_a_run_scoped_record_declared_in_models_may_be_saved(tmp_path: Path) -> None:
    target = tmp_path / "m.py"
    target.write_text(
        "from app.models.records.workflow_output import WorkflowOutput\n"
        "WorkflowOutput(...).save()\n",
        encoding="utf-8",
    )
    assert find_persisted_write_call_offenders([target]) == []


def test_a_project_scoped_record_may_still_not_be_saved(tmp_path: Path) -> None:
    target = tmp_path / "m.py"
    target.write_text(
        "from app.models.records.project import Project\nProject(...).save()\n",
        encoding="utf-8",
    )
    assert find_persisted_write_call_offenders([target]) == ["m.py: ['save']"]

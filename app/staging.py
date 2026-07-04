"""
staging.py — a per-project STAGING store for DATA-MODEL edits.

WHY THIS EXISTS
───────────────
The authoring AI has in-process edit-tools (app/edit_tools.py) that make TARGETED
edits to the data model (the named schemas). Those edits must NOT go straight to
disk: every change is a proposal a human reviews (Save) or throws away (Discard).
This module is that holding area — the single place a staged edit lives between the
moment a tool stages it (during an SSE authoring stream) and the moment a human
POSTs Save (a separate later request). Because those are two different HTTP
requests, the store is PERSISTED to a file next to the project:

    <project_dir>/data_model_staging.json

Shape on disk (stable — the UI and the apply step read it):

    {"schemas": {"<schema_name>": <full edited schema dict>, ...}}

A staged edit is the WHOLE edited schema dict (not a patch): the first time a
schema is touched we SEED the staged copy by reading the current on-disk YAML,
then mutate that copy. Seeding-from-disk means a diff is always "staged vs disk"
and applying is a plain file write — no patch replay, nothing to get out of sync.

CARDINAL RULE (non-negotiable): NEVER fabricate / no silent fallback.
  - Every mutator that targets a schema or a column that does not exist RAISES
    (StagingError). It never no-ops, never invents the target. The caller (an
    edit-tool) turns that raise into an error string the MODEL sees.
  - apply_staged VALIDATES every staged schema with models.validate_named_schema
    and RAISES if any is invalid — it never writes a junk schema to disk.

DEPENDENCY RULE (mirrors app/models + app/services discipline): this module imports
only stdlib + yaml + app.models + app.compiler.chat's schema file-writer. It does
NOT import app.runtime.* and does NOT import the SDK — it is a pure, side-effect-light
store the edit-tools and the routes layer both lean on.

The on-disk schema reads mirror app.web.loading.load_schemas: a schemas/*.yaml file
may hold one OR many documents (multi-doc YAML), so we scan with safe_load_all and
match on each doc's `name`. Loader bookkeeping keys (_filename/_error) are never in
the files we read directly, so a staged schema is the spec only.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import yaml

from app import models

# ── The staging file, and the on-disk schemas dir the working copy seeds from ──
# Both hang off the project working copy (examples/<name>/), the SAME dir the
# compiler persists authored schemas to (app/compiler/chat.py:_persist_schema) and
# the data-model view reads them from.

STAGING_FILENAME = "data_model_staging.json"


class StagingError(Exception):
    """A staged edit could not be made because its target does not exist (missing
    schema or column), or a Save was attempted on an invalid staged schema. Raised
    LOUDLY rather than silently no-op'd — the edit-tool surfaces it to the model,
    and apply_staged surfaces it to the Save route as a refusal. This is the
    fail-loud contract; there is no silent fallback path."""


# ─────────────────────────────────────────────────────────────────────────────
# 1. Locations + load/save of the store
# ─────────────────────────────────────────────────────────────────────────────

def staging_path(project_dir: str | Path) -> Path:
    """<project_dir>/data_model_staging.json — the single per-project staging file.
    One file per project, so concurrent projects never share staged edits."""
    return Path(project_dir) / STAGING_FILENAME


def load_staging(project_dir: str | Path) -> dict[str, Any]:
    """Load the staging store, or an empty, correctly-shaped store when none exists
    yet. Shape: {"schemas": {name: <edited schema dict>}}. A corrupt/legacy file
    (not the expected mapping) is treated as empty rather than crashing the stream —
    but note NOTHING is silently discarded on disk until an explicit clear/apply."""
    p = staging_path(project_dir)
    if not p.exists():
        return {"schemas": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"schemas": {}}
    if not isinstance(data, dict) or not isinstance(data.get("schemas"), dict):
        return {"schemas": {}}
    return data


def _save_staging(project_dir: str | Path, store: dict[str, Any]) -> None:
    """Persist the staging store to disk (pretty JSON, UTF-8). Creates the project
    dir if missing."""
    p = staging_path(project_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(store, ensure_ascii=False, indent=2, sort_keys=False),
        encoding="utf-8",
    )


def has_staged_edits(project_dir: str | Path) -> bool:
    """True iff at least one schema currently has a staged edit."""
    return bool(load_staging(project_dir).get("schemas"))


# ─────────────────────────────────────────────────────────────────────────────
# 2. On-disk schema access (the seed source) + the working copy
# ─────────────────────────────────────────────────────────────────────────────

def _schemas_dir(project_dir: str | Path) -> Path:
    """examples/<name>/schemas — where authored schema YAMLs live (the same dir
    app/compiler/chat.py:_persist_schema writes to)."""
    return Path(project_dir) / "schemas"


def _iter_disk_schemas(project_dir: str | Path) -> list[tuple[Path, dict[str, Any]]]:
    """(file, schema-dict) for every on-disk schema, in filename order. A file may
    hold one OR many docs (multi-doc YAML) — mirror app.web.loading.load_schemas
    (safe_load_all) so we see the SAME set the rest of the app does. A YAML parse
    error skips that file rather than crashing (the data-model view surfaces the
    error separately)."""
    out: list[tuple[Path, dict[str, Any]]] = []
    schemas_dir = _schemas_dir(project_dir)
    if not schemas_dir.is_dir():
        return out
    for yaml_file in sorted(schemas_dir.glob("*.yaml")):
        try:
            with yaml_file.open("r", encoding="utf-8") as f:
                for doc in yaml.safe_load_all(f):
                    if isinstance(doc, dict):
                        out.append((yaml_file, doc))
        except yaml.YAMLError:
            continue
    return out


def _find_schema_file(project_dir: str | Path, schema_name: str) -> Path | None:
    """Locate the on-disk YAML file whose schema `name` == schema_name, matching on
    the loaded dict's `name` (not the filename), the SAME convention the schema-edit
    writer uses. Returns None if no such schema is on disk."""
    for fpath, data in _iter_disk_schemas(project_dir):
        if data.get("name") == schema_name:
            return fpath
    return None


def load_disk_schema(project_dir: str | Path, schema_name: str) -> dict[str, Any] | None:
    """The current ON-DISK schema dict for schema_name, or None if it is not on disk.
    Loader bookkeeping keys are not injected here (we read the files directly), so the
    returned dict is exactly what was written."""
    for _fpath, data in _iter_disk_schemas(project_dir):
        if data.get("name") == schema_name:
            return data
    return None


def load_all_disk_schemas(project_dir: str | Path) -> list[dict[str, Any]]:
    """Every on-disk schema (in filename order). Mirrors app.web.loading.load_schemas'
    read so diffs/validation see the SAME set the rest of the app does (bookkeeping
    keys aside — those are added by load_schemas, not here)."""
    return [data for _fpath, data in _iter_disk_schemas(project_dir)]


def get_working_schema(project_dir: str | Path, schema_name: str) -> dict[str, Any]:
    """The WORKING copy of a schema: the STAGED edit if one exists, else the current
    ON-DISK schema. Returns a deep copy so a caller mutating it never touches the
    stored dict by reference.

    FAILS LOUD (StagingError) if the schema is neither staged nor on disk — an edit
    can only target a schema that actually exists; we never fabricate one."""
    store = load_staging(project_dir)
    staged = store.get("schemas", {}).get(schema_name)
    if staged is not None:
        return copy.deepcopy(staged)
    disk = load_disk_schema(project_dir, schema_name)
    if disk is not None:
        return copy.deepcopy(disk)
    raise StagingError(
        f"schema '{schema_name}' does not exist in this project "
        f"(not staged and no file in {_schemas_dir(project_dir).as_posix()}). "
        f"Cannot edit a schema that is not present."
    )


def _stage_schema(project_dir: str | Path, schema: dict[str, Any]) -> None:
    """Write the (edited) full schema dict into the store under its `name` and
    persist. The schema's `name` is authoritative for the store key."""
    name = schema.get("name")
    if not name:
        raise StagingError("cannot stage a schema with no 'name'")
    store = load_staging(project_dir)
    store.setdefault("schemas", {})[name] = schema
    _save_staging(project_dir, store)


# ── Column helpers (columns are plain dicts once loaded from YAML) ─────────────

def _columns(schema: dict[str, Any]) -> list[dict[str, Any]]:
    """The schema's columns list (creating an empty one if absent, so add-column
    works on a schema authored with no columns yet). Mutates schema in place."""
    cols = schema.get("columns")
    if cols is None:
        cols = []
        schema["columns"] = cols
    if not isinstance(cols, list):
        raise StagingError(
            f"schema '{schema.get('name')}' has a non-list 'columns' "
            f"({type(cols).__name__}); refusing to edit a malformed schema"
        )
    return cols


def _find_column(schema: dict[str, Any], column_name: str) -> dict[str, Any]:
    """The column dict named column_name, or RAISE (fail loud) if the schema has no
    such column. Never returns None / a fabricated column."""
    for c in _columns(schema):
        if isinstance(c, dict) and c.get("name") == column_name:
            return c
    have = [c.get("name") for c in _columns(schema) if isinstance(c, dict)]
    raise StagingError(
        f"column '{column_name}' does not exist in schema '{schema.get('name')}' "
        f"(columns present: {have}). Cannot edit a column that is not present."
    )


# ─────────────────────────────────────────────────────────────────────────────
# 3. MUTATORS — each seeds-from-disk (via get_working_schema) then mutates the
#    staged copy, then persists. Every one FAILS LOUD on a missing target.
#    Return the updated working schema dict (so a tool can summarise the change).
# ─────────────────────────────────────────────────────────────────────────────

def set_column_type(
    project_dir: str | Path, schema_name: str, column_name: str, new_type: str
) -> dict[str, Any]:
    """Change one column's `type`. Raises if the schema or column is missing. Does
    NOT itself reject an unknown type string — validity is surfaced by staged_diffs
    (via validate_named_schema) and enforced at apply_staged, so the model still
    sees the type it asked for staged and gets a validation issue rather than a
    silent correction."""
    schema = get_working_schema(project_dir, schema_name)
    col = _find_column(schema, column_name)
    col["type"] = new_type
    _stage_schema(project_dir, schema)
    return schema


def add_column(
    project_dir: str | Path,
    schema_name: str,
    column_name: str,
    *,
    type: str = "str",
    nullable: bool = True,
    description: str | None = None,
    references: str | None = None,
) -> dict[str, Any]:
    """Add a NEW column. Raises if the schema is missing OR if a column of that name
    already exists (adding a duplicate is a mistake, not a silent overwrite — use
    set_column_type / set_column_description to change an existing one)."""
    schema = get_working_schema(project_dir, schema_name)
    cols = _columns(schema)
    if any(isinstance(c, dict) and c.get("name") == column_name for c in cols):
        raise StagingError(
            f"column '{column_name}' already exists in schema '{schema_name}'; "
            f"use set_column_type/description to change it, not add_column"
        )
    new_col: dict[str, Any] = {"name": column_name, "type": type, "nullable": nullable}
    if description is not None:
        new_col["description"] = description
    if references is not None:
        new_col["references"] = references
    cols.append(new_col)
    _stage_schema(project_dir, schema)
    return schema


def remove_column(
    project_dir: str | Path, schema_name: str, column_name: str
) -> dict[str, Any]:
    """Remove a column. Raises if the schema or column is missing. Also drops the
    column from `primary_key` if it was part of it (leaving a PK that names a
    now-absent column would be an invalid schema — apply would refuse it — so we
    keep the staged copy internally consistent)."""
    schema = get_working_schema(project_dir, schema_name)
    _find_column(schema, column_name)  # fail loud if absent
    schema["columns"] = [
        c for c in _columns(schema)
        if not (isinstance(c, dict) and c.get("name") == column_name)
    ]
    pk = schema.get("primary_key")
    if isinstance(pk, list) and column_name in pk:
        schema["primary_key"] = [k for k in pk if k != column_name]
    _stage_schema(project_dir, schema)
    return schema


def rename_column(
    project_dir: str | Path, schema_name: str, old_name: str, new_name: str
) -> dict[str, Any]:
    """Rename a column old_name -> new_name (updating a primary_key membership too).
    Raises if the schema or old_name is missing, or if new_name already names
    another column (a rename must not silently merge two columns)."""
    schema = get_working_schema(project_dir, schema_name)
    col = _find_column(schema, old_name)  # fail loud if absent
    if new_name != old_name and any(
        isinstance(c, dict) and c.get("name") == new_name for c in _columns(schema)
    ):
        raise StagingError(
            f"cannot rename '{old_name}' to '{new_name}' in schema '{schema_name}': "
            f"a column named '{new_name}' already exists"
        )
    col["name"] = new_name
    pk = schema.get("primary_key")
    if isinstance(pk, list):
        schema["primary_key"] = [new_name if k == old_name else k for k in pk]
    _stage_schema(project_dir, schema)
    return schema


def set_column_description(
    project_dir: str | Path, schema_name: str, column_name: str, description: str
) -> dict[str, Any]:
    """Set a column's `description`. Raises if the schema or column is missing."""
    schema = get_working_schema(project_dir, schema_name)
    col = _find_column(schema, column_name)
    col["description"] = description
    _stage_schema(project_dir, schema)
    return schema


def set_schema_description(
    project_dir: str | Path, schema_name: str, description: str
) -> dict[str, Any]:
    """Set the schema-level `description`. Raises if the schema is missing."""
    schema = get_working_schema(project_dir, schema_name)
    schema["description"] = description
    _stage_schema(project_dir, schema)
    return schema


def set_primary_key(
    project_dir: str | Path, schema_name: str, primary_key: list[str]
) -> dict[str, Any]:
    """Set the schema's `primary_key` (a list of column names). Raises if the schema
    is missing OR if any named column is not a declared column of the schema — a PK
    that names a phantom column is invalid, so we refuse it loudly at stage time
    rather than let it slip to apply."""
    schema = get_working_schema(project_dir, schema_name)
    declared = {c.get("name") for c in _columns(schema) if isinstance(c, dict)}
    missing = [k for k in primary_key if k not in declared]
    if missing:
        raise StagingError(
            f"cannot set primary_key {primary_key} on schema '{schema_name}': "
            f"column(s) {missing} are not declared on the schema"
        )
    schema["primary_key"] = list(primary_key)
    _stage_schema(project_dir, schema)
    return schema


# ─────────────────────────────────────────────────────────────────────────────
# 4. DIFFS — per staged schema, a node/key/value diff vs on-disk + validity
# ─────────────────────────────────────────────────────────────────────────────

# Schema-level scalar attributes we surface as key/value changes (columns are
# handled separately as node changes). Kept explicit so a diff shows exactly the
# fields a human cares about, in a stable order.
_SCHEMA_ATTRS: tuple[str, ...] = (
    "title", "description", "kind", "primary_key", "notes",
)


def _column_index(schema: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """{column_name: column_dict} for a schema (empty for None / no columns)."""
    if not isinstance(schema, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for c in schema.get("columns") or []:
        if isinstance(c, dict) and c.get("name"):
            out[c["name"]] = c
    return out


def _diff_one_schema(
    disk: dict[str, Any] | None, staged: dict[str, Any]
) -> dict[str, Any]:
    """Node/key/value diff of ONE staged schema vs its on-disk version.

    Returns:
        {
          "schema": <name>,
          "is_new": <bool>,               # staged schema has no on-disk file yet
          "columns_added":   [<col dict>, ...],
          "columns_removed": [<col dict>, ...],
          "columns_changed": [
             {"name": <col>, "keys": [{"key","before","after"}, ...]}, ...
          ],
          "attr_changes": [{"key","before","after"}, ...],   # schema-level scalars
          "issues": [<str>, ...],         # validate_named_schema on the STAGED dict
        }
    The three column buckets are the NODE-level diff; attr_changes + per-column keys
    are the KEY/VALUE-level diff — matching the "node/key/value" contract."""
    disk_cols = _column_index(disk)
    staged_cols = _column_index(staged)

    added = [staged_cols[n] for n in staged_cols if n not in disk_cols]
    removed = [disk_cols[n] for n in disk_cols if n not in staged_cols]

    changed: list[dict[str, Any]] = []
    for name in staged_cols:
        if name not in disk_cols:
            continue
        before_col, after_col = disk_cols[name], staged_cols[name]
        key_changes: list[dict[str, Any]] = []
        for key in sorted(set(before_col) | set(after_col)):
            b, a = before_col.get(key), after_col.get(key)
            if b != a:
                key_changes.append({"key": key, "before": b, "after": a})
        if key_changes:
            changed.append({"name": name, "keys": key_changes})

    attr_changes: list[dict[str, Any]] = []
    disk_attrs = disk or {}
    for key in _SCHEMA_ATTRS:
        b, a = disk_attrs.get(key), staged.get(key)
        if b != a:
            attr_changes.append({"key": key, "before": b, "after": a})

    return {
        "schema": staged.get("name"),
        "is_new": disk is None,
        "columns_added": added,
        "columns_removed": removed,
        "columns_changed": changed,
        "attr_changes": attr_changes,
        "issues": models.validate_named_schema(staged),
    }


def staged_diffs(project_dir: str | Path) -> dict[str, Any]:
    """The full staged diff for a project: one node/key/value diff per staged schema,
    vs the current on-disk version, PLUS validate_named_schema issues per schema (so
    the UI/apply can show/refuse invalid staged edits).

    Returns:
        {
          "schemas": [ <per-schema diff, see _diff_one_schema>, ... ],
          "any_invalid": <bool>,   # true if ANY staged schema has validation issues
          "count": <int>,          # number of staged schemas
        }
    An empty store yields {"schemas": [], "any_invalid": False, "count": 0}."""
    store = load_staging(project_dir)
    staged_map = store.get("schemas", {})
    diffs: list[dict[str, Any]] = []
    for name in sorted(staged_map):
        staged = staged_map[name]
        disk = load_disk_schema(project_dir, name)
        diffs.append(_diff_one_schema(disk, staged))
    any_invalid = any(d["issues"] for d in diffs)
    return {"schemas": diffs, "any_invalid": any_invalid, "count": len(diffs)}


def diff_summary_line(project_dir: str | Path, schema_name: str) -> str:
    """A ONE-LINE human summary of the staged change for schema_name (for a tool's
    confirmation text). E.g. "schema 'company': 1 column changed, 1 added". Reflects
    the CURRENT staged-vs-disk diff, so it is accurate after several edits, not just
    the last one. Returns a '(no staged changes)' marker if nothing differs."""
    store = load_staging(project_dir)
    staged = store.get("schemas", {}).get(schema_name)
    if staged is None:
        return f"schema '{schema_name}': (nothing staged)"
    disk = load_disk_schema(project_dir, schema_name)
    d = _diff_one_schema(disk, staged)
    parts: list[str] = []
    if d["is_new"]:
        parts.append("NEW schema")
    if d["columns_added"]:
        parts.append(f"{len(d['columns_added'])} column(s) added")
    if d["columns_removed"]:
        parts.append(f"{len(d['columns_removed'])} column(s) removed")
    if d["columns_changed"]:
        parts.append(f"{len(d['columns_changed'])} column(s) changed")
    if d["attr_changes"]:
        keys = ", ".join(c["key"] for c in d["attr_changes"])
        parts.append(f"attr(s) changed: {keys}")
    summary = "; ".join(parts) if parts else "(no staged changes)"
    issues = d["issues"]
    if issues:
        summary += f"  [!] {len(issues)} validation issue(s): {issues[0]}"
    return f"schema '{schema_name}': {summary}"


# ─────────────────────────────────────────────────────────────────────────────
# 5. APPLY / DISCARD
# ─────────────────────────────────────────────────────────────────────────────

def apply_staged(project_dir: str | Path) -> dict[str, Any]:
    """COMMIT every staged schema to disk (the human's Save).

    Discipline (mirrors the schema-edit route — validate then write, never write
    junk): FIRST validate EVERY staged schema with validate_named_schema. If ANY is
    invalid, RAISE StagingError with the full issue list and write NOTHING — an
    invalid data model must never reach disk. Only if all are clean do we write each
    via the SAME NN_<name>.yaml convention app/compiler/chat.py:_persist_schema uses
    (overwriting the schema's existing file in place), then CLEAR the store.

    Returns {"written": [<path>, ...], "schemas": [<name>, ...]} on success.

    NOTE on the approval gate: writing the schema files changes the schema-library
    content hash, so a prior data_model approval auto-drops to edited_stale (see
    app.services.node_review.data_model_state). That flip is a SIDE EFFECT of the
    write; this function does not touch node_decisions itself."""
    from app.compiler.chat import _persist_schema  # local import: avoid import cycle

    store = load_staging(project_dir)
    staged_map = store.get("schemas", {})
    if not staged_map:
        return {"written": [], "schemas": []}

    # 1) Validate ALL before writing ANY (fail loud on the first junk schema).
    all_issues: dict[str, list[str]] = {}
    for name in sorted(staged_map):
        issues = models.validate_named_schema(staged_map[name])
        if issues:
            all_issues[name] = issues
    if all_issues:
        detail = "; ".join(
            f"{name}: {', '.join(iss)}" for name, iss in all_issues.items()
        )
        raise StagingError(
            f"refusing to save — {len(all_issues)} staged schema(s) are invalid: "
            f"{detail}"
        )

    # 2) All clean → write each via the compiler's NN_<name>.yaml writer, which
    #    overwrites the schema's existing file in place (same convention the whole
    #    app reads). project_dir is the base the schemas/ dir hangs off.
    base = Path(project_dir)
    written: list[str] = []
    names: list[str] = []
    for name in sorted(staged_map):
        path = _persist_schema(base, staged_map[name])
        written.append(path)
        names.append(name)

    # 3) Clear the store — the proposals are now the truth on disk.
    clear_staging(project_dir)
    return {"written": written, "schemas": names}


def clear_staging(project_dir: str | Path) -> None:
    """Discard ALL staged edits (the human's Discard, and the post-apply reset).
    Removes the staging file entirely so has_staged_edits() is False afterward."""
    p = staging_path(project_dir)
    if p.exists():
        p.unlink()


def discard_schema(project_dir: str | Path, schema_name: str) -> bool:
    """Discard the staged edit for ONE schema (leaving others staged). Returns True
    if there was a staged edit to drop, False if there was nothing staged for it."""
    store = load_staging(project_dir)
    schemas = store.get("schemas", {})
    if schema_name not in schemas:
        return False
    del schemas[schema_name]
    _save_staging(project_dir, store)
    return True


__all__ = [
    "STAGING_FILENAME",
    "StagingError",
    "staging_path",
    "load_staging",
    "has_staged_edits",
    "load_disk_schema",
    "load_all_disk_schemas",
    "get_working_schema",
    "set_column_type",
    "add_column",
    "remove_column",
    "rename_column",
    "set_column_description",
    "set_schema_description",
    "set_primary_key",
    "staged_diffs",
    "diff_summary_line",
    "apply_staged",
    "clear_staging",
    "discard_schema",
]

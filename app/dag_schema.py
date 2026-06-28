"""
dag_schema.py — THE canonical contract for a methodology DAG.

This is the single clean interface between the DAG artifact and everything built
on top of it (compiler · runner · eval). It is the one place that defines what a
valid stage and a valid DAG look like:

  - the 7 node types and the executable-handle block each one requires
  - the universal stage keys
  - the column-type vocabulary, connector kinds, aggregation formulas
  - structural validators for a stage and for a whole DAG

Dependency rule (deliberate): this module imports NOTHING from the runtime or the
compiler. The runtime imports THIS to validate/execute; the compiler imports THIS
to know what to emit. Neither imports the other — they only meet here. Keep this
file pure data + pure functions so it stays a trustworthy interface.

This is the STAGE-SPEC contract (is the YAML well-formed). It is distinct from
runtime DATA validation (`app/runtime/validation.py`, which checks dataframes
against an output_schema at run time).
"""

from __future__ import annotations

import re
from typing import Any

# ── Column-type vocabulary ───────────────────────────────────────────────────
# Scalar types plus the `list[<type>]` / `dict` / `json` containers.
SCALAR_COLUMN_TYPES: set[str] = {
    "str", "int", "float", "bool", "datetime", "date", "dict", "json",
}
NUMERIC_COLUMN_TYPES: set[str] = {"int", "float"}
_LIST_RE = re.compile(r"^list\[(.+)\]$")


def is_valid_column_type(t: str) -> bool:
    if not isinstance(t, str):
        return False
    if t in SCALAR_COLUMN_TYPES:
        return True
    m = _LIST_RE.match(t)
    if m:
        inner = m.group(1).strip()
        return inner in SCALAR_COLUMN_TYPES or bool(_LIST_RE.match(inner))
    return False


# ── Connector kinds (input_data) ─────────────────────────────────────────────
CONNECTOR_KINDS: set[str] = {
    "file", "http", "scrape", "api", "manual_upload", "sql", "computed_static",
}
# What the demo runtime can actually execute today; the rest raise
# NotImplementedError (see handlers.handle_input_data). The compiler may emit any
# kind, but a stage using an unimplemented one is flagged as not-yet-runnable.
IMPLEMENTED_CONNECTOR_KINDS: set[str] = {"file", "computed_static"}
FILE_FORMATS: set[str] = {"csv", "parquet", "json", "geojson"}

# ── Aggregation formulas (aggregate) ─────────────────────────────────────────
AGG_FORMULAS: set[str] = {
    "sum", "mean", "count", "min", "max", "first", "list",
    "weighted_mean", "weighted_sum",
}
WEIGHTED_FORMULAS: set[str] = {"weighted_mean", "weighted_sum"}

# ── Join + review vocab ──────────────────────────────────────────────────────
JOIN_TYPES: set[str] = {"inner", "left", "right", "outer"}
FUNCTION_KINDS: set[str] = {"module", "inline"}
PUBLISH_FORMATS: set[str] = {"html_report", "json", "csv", "evidence_cards"}
ROUTING_OPTIONS: set[str] = {
    "1-of-1", "2-of-2", "1-of-2-disagreement-escalates",
    "random_sample_10pct", "single_reviewer", "third_reviewer",
}

# ── Universal stage keys (any type) ──────────────────────────────────────────
UNIVERSAL_KEYS: set[str] = {
    "id", "name", "type", "source", "inputs", "output_schema",
    "limit", "compiler_notes", "eval", "review",
}

# ── The seven node types and their contract ──────────────────────────────────
# For each type:
#   handle           : the executable-handle block key it must carry
#   also_requires    : extra block(s) it also needs (publish runs a function)
#   requires_inputs  : must declare upstream inputs
#   min_inputs       : minimum number of inputs
#   required / optional : fields inside the handle block
NODE_TYPES: dict[str, dict[str, Any]] = {
    "input_data": {
        "summary": "Declares a source dataset with a typed schema.",
        "handle": "connector",
        "requires_inputs": False,
        "min_inputs": 0,
        "required": ["kind"],
        "optional": ["params", "refresh", "notes"],
    },
    "llm_transform": {
        "summary": "Row-by-row LLM call producing structured output.",
        "handle": "llm",
        "requires_inputs": True,
        "min_inputs": 1,
        "required": ["prompt_template"],
        "optional": ["model", "temperature", "response_format", "max_retries",
                     "rubric", "tools"],
    },
    "python_transform": {
        "summary": "Arbitrary Python over upstream dataframes.",
        "handle": "function",
        "requires_inputs": True,
        "min_inputs": 1,
        "required": ["kind"],
        "optional": ["module", "function", "code", "requirements"],
    },
    "join": {
        "summary": "Combine two or more upstream dataframes on keys.",
        "handle": "join",
        "requires_inputs": True,
        "min_inputs": 2,
        "required": ["keys"],
        "optional": ["type", "select", "on"],
    },
    "aggregate": {
        "summary": "Structured group-by aggregation.",
        "handle": "aggregate",
        "requires_inputs": True,
        "min_inputs": 1,
        "required": ["group_by", "aggregations"],
        "optional": [],
    },
    "human_review_queue": {
        "summary": "Pulls flagged rows for human decision; halts the run.",
        "handle": "queue",
        "requires_inputs": True,
        "min_inputs": 1,
        "required": [],
        "optional": ["filter", "hash_columns", "reviewer_instructions",
                     "routing", "conflict_resolution", "estimated_volume_per_week"],
    },
    "publish": {
        "summary": "Render a final artifact (html, json, csv, cards).",
        "handle": "publish",
        "also_requires": ["function"],
        "requires_inputs": True,
        "min_inputs": 1,
        "required": [],
        "optional": ["format", "destination", "template", "one_file_per", "cross_link"],
    },
}

NODE_TYPE_NAMES: set[str] = set(NODE_TYPES)


# ── Validators ───────────────────────────────────────────────────────────────

def validate_table_schema(schema: dict[str, Any] | None, where: str) -> list[str]:
    """Validate a TableSchema (the `output_schema` or an input `schema`)."""
    issues: list[str] = []
    if schema is None:
        return issues
    cols = schema.get("columns")
    if cols is None:
        issues.append(f"{where}: schema has no `columns`")
        return issues
    seen: set[str] = set()
    for i, col in enumerate(cols):
        if not isinstance(col, dict):
            issues.append(f"{where}: column[{i}] is not a mapping")
            continue
        name = col.get("name")
        if not name:
            issues.append(f"{where}: column[{i}] missing `name`")
        elif name in seen:
            issues.append(f"{where}: duplicate column `{name}`")
        else:
            seen.add(name)
        ctype = col.get("type", "str")
        if not is_valid_column_type(ctype):
            issues.append(f"{where}: column `{name}` has unknown type `{ctype}`")
    pk = schema.get("primary_key")
    if pk is not None:
        if not isinstance(pk, list):
            issues.append(f"{where}: primary_key must be a list")
        else:
            for k in pk:
                if k not in seen:
                    issues.append(f"{where}: primary_key `{k}` is not a declared column")
    return issues


# ── Named schemas (the data model, authored BEFORE the DAG) ──────────────────
#
# A `TableSchema` (above) is anonymous: it lives inline on a stage's
# `output_schema` or an input. A **named schema** promotes that same shape to a
# first-class, addressable artifact that exists independent of any stage — the
# data model. A methodology declares a *library* of named schemas in
# `examples/<name>/schemas/*.yaml`, and the DAG (authored second) wires
# transforms between them. This inverts the old order (DAG-first, data-model
# derived from stage outputs) into data-model-first.
#
# A named schema is a TableSchema plus:
#   name   : snake_case identity, referenced by stages via `schema_ref`
#   kind   : where the table sits in the pipeline (the distinction the
#            DAG-derived view could not express)
#   columns[].references : a foreign key to another named schema (by name),
#            optionally "schema.column" — makes the data model a real graph
#            rather than relying on PK-name-collision heuristics.

SCHEMA_KINDS: set[str] = {
    "reference",     # dimension / lookup / benchmark data we must source, not compute
    "input",         # raw data fetched into the pipeline
    "computed",      # produced by a DAG stage
    "ground_truth",  # external truth used only by eval (mirrors a computed schema)
}

NAMED_SCHEMA_KEYS: set[str] = {
    "name", "title", "kind", "description", "source",
    "columns", "primary_key", "estimated_rows", "notes",
    "produced_by", "consumed_by", "exclusive_arcs",
}


def _parse_reference(ref: str) -> tuple[str, str | None]:
    """`"company"` → (company, None); `"company.company_id"` → (company, company_id)."""
    if "." in ref:
        schema_name, col = ref.split(".", 1)
        return schema_name.strip(), col.strip()
    return ref.strip(), None


def validate_named_schema(schema: dict[str, Any], where: str | None = None) -> list[str]:
    """Validate ONE named schema dict (a TableSchema + name/kind/references).
    Cross-schema reference resolution is checked by validate_schema_library."""
    name = schema.get("name")
    where = where or (f"schema `{name}`" if name else "schema <no-name>")
    issues: list[str] = []
    if not name or not isinstance(name, str):
        issues.append(f"{where}: missing a string `name`")
    elif not re.match(r"^[a-z][a-z0-9_]*$", name):
        issues.append(f"`{name}`: name should be snake_case")

    kind = schema.get("kind")
    if kind not in SCHEMA_KINDS:
        issues.append(f"{where}: kind `{kind}` must be one of {sorted(SCHEMA_KINDS)}")

    # The column/PK shape is the same contract as an inline TableSchema.
    issues += validate_table_schema(schema, where)

    # `references` on a column must name a string (resolution checked library-wide).
    for col in schema.get("columns") or []:
        if isinstance(col, dict) and col.get("references") is not None:
            if not isinstance(col["references"], str):
                issues.append(f"{where}: column `{col.get('name')}` references must be a string")

    # Exclusive arcs: "exactly one of these columns is non-null per row" — the XOR
    # foreign key (e.g. a cell scores a company XOR an influencer). Each arc column
    # must be declared and nullable (since each may be the one that is null). The
    # exactly-one-set check on actual rows is a runtime DATA validation.
    col_by_name = {c.get("name"): c for c in schema.get("columns") or [] if isinstance(c, dict)}
    for arc in schema.get("exclusive_arcs") or []:
        if not isinstance(arc, list) or len(arc) < 2:
            issues.append(f"{where}: each exclusive_arc must list >= 2 columns")
            continue
        for cname in arc:
            col = col_by_name.get(cname)
            if col is None:
                issues.append(f"{where}: exclusive_arc column `{cname}` is not declared")
            elif col.get("nullable") is False:
                issues.append(f"{where}: exclusive_arc column `{cname}` must be nullable (exactly one is set)")
    return issues


def validate_schema_library(schemas: list[dict[str, Any]]) -> list[str]:
    """Validate a whole data model: each schema valid, names unique, and every
    column `references` resolves to a real schema (and column, if given)."""
    issues: list[str] = []
    for s in schemas:
        issues += validate_named_schema(s)

    names = [s.get("name") for s in schemas if s.get("name")]
    for d in sorted({n for n in names if names.count(n) > 1}):
        issues.append(f"duplicate schema name `{d}`")

    by_name: dict[str, dict[str, Any]] = {s["name"]: s for s in schemas if s.get("name")}
    for s in schemas:
        sname = s.get("name", "<no-name>")
        for col in s.get("columns") or []:
            if not (isinstance(col, dict) and col.get("references")):
                continue
            target_name, target_col = _parse_reference(col["references"])
            target = by_name.get(target_name)
            if target is None:
                issues.append(
                    f"`{sname}`.{col.get('name')}: references unknown schema `{target_name}`")
                continue
            if target_col is not None:
                target_cols = {c.get("name") for c in target.get("columns") or []}
                if target_col not in target_cols:
                    issues.append(
                        f"`{sname}`.{col.get('name')}: references `{target_name}.{target_col}` "
                        f"which is not a column of `{target_name}`")
    return issues


# ── Eval data model (SEPARATE from generation; derives FROM it) ──────────────
#
# Ground truth for the eval is NOT part of the generation data model — eval is a
# consumer of generation, one-directional, and generation has no knowledge of it.
# An eval spec (examples/<name>/eval/*.yaml) names the generation schema it grades
# (`evaluates`) and the columns it mirrors; the ground-truth schema is DERIVED
# from that generation schema so it is consistent with generation BY CONSTRUCTION
# — it cannot silently drift from what the pipeline produces.

def build_ground_truth_schema(eval_spec: dict[str, Any],
                              gen_schema: dict[str, Any]) -> dict[str, Any]:
    """Derive the ground-truth named schema from the GENERATION schema it grades.
    Mirrored columns ARE the generation columns (consistency by construction);
    eval-only `extra_columns` are appended."""
    gen_cols = {c["name"]: c for c in gen_schema.get("columns") or [] if c.get("name")}
    mirror = eval_spec.get("mirror_columns") or list(gen_cols)
    columns = [gen_cols[name] for name in mirror if name in gen_cols]
    columns += eval_spec.get("extra_columns") or []
    pk = [k for k in (gen_schema.get("primary_key") or []) if k in mirror]
    # Inherit any exclusive arc whose columns are all mirrored, so the ground
    # truth carries the same XOR constraint as what it grades.
    arcs = [a for a in (gen_schema.get("exclusive_arcs") or []) if all(c in mirror for c in a)]
    return {
        "name": eval_spec.get("name"),
        "kind": "ground_truth",
        "evaluates": eval_spec.get("evaluates"),
        "columns": columns,
        "primary_key": pk or None,
        "exclusive_arcs": arcs or None,
        "notes": eval_spec.get("notes"),
    }


def validate_eval_spec(eval_spec: dict[str, Any],
                       gen_by_name: dict[str, dict[str, Any]]) -> list[str]:
    """Ensure an eval spec is consistent with the generation data model: it grades
    a real generation schema, mirrors/joins on real columns of it, and adds no
    eval-only column that collides with a generation column."""
    issues: list[str] = []
    name = eval_spec.get("name", "<no-name>")
    target = eval_spec.get("evaluates")
    gen = gen_by_name.get(target)
    if gen is None:
        issues.append(f"eval `{name}`: evaluates unknown generation schema `{target}`")
        return issues
    gen_cols = {c.get("name") for c in gen.get("columns") or []}
    for key in ("mirror_columns", "join_on"):
        for col in eval_spec.get(key) or []:
            if col not in gen_cols:
                issues.append(f"eval `{name}`.{key}: `{col}` is not a column of `{target}`")
    for col in {c.get("name") for c in eval_spec.get("extra_columns") or []} & gen_cols:
        issues.append(f"eval `{name}`: extra column `{col}` collides with generation `{target}`")
    if not eval_spec.get("metrics"):
        issues.append(f"eval `{name}`: declares no metrics")
    return issues


def validate_stage(stage: dict[str, Any]) -> list[str]:
    """Structural validation of ONE compiled stage dict against the contract.
    Returns a list of human-readable issues ([] means valid)."""
    issues: list[str] = []
    sid = stage.get("id")
    if not sid or not isinstance(sid, str):
        issues.append("stage missing a string `id`")
        sid = sid or "<no-id>"
    elif not re.match(r"^[a-z][a-z0-9_]*$", sid):
        issues.append(f"`{sid}`: id should be snake_case")

    stype = stage.get("type")
    if stype not in NODE_TYPES:
        issues.append(f"`{sid}`: unknown type `{stype}` (must be one of {sorted(NODE_TYPE_NAMES)})")
        return issues  # can't validate the handle without a known type
    spec = NODE_TYPES[stype]

    # Executable handle block present
    handle = spec["handle"]
    block = stage.get(handle)
    if not isinstance(block, dict):
        issues.append(f"`{sid}`: type `{stype}` requires a `{handle}:` block")
        block = {}
    for extra in spec.get("also_requires", []):
        if not isinstance(stage.get(extra), dict):
            issues.append(f"`{sid}`: type `{stype}` also requires a `{extra}:` block")

    # Required block fields
    for field in spec["required"]:
        if field not in block:
            issues.append(f"`{sid}`: `{handle}.{field}` is required")

    # Inputs
    inputs = stage.get("inputs") or []
    if spec["requires_inputs"] and len(inputs) < spec["min_inputs"]:
        issues.append(f"`{sid}`: type `{stype}` needs >= {spec['min_inputs']} input(s), got {len(inputs)}")
    for inp in inputs:
        if isinstance(inp, dict) and inp.get("schema"):
            issues += validate_table_schema(inp["schema"], f"`{sid}` input `{inp.get('id')}`")

    # Output schema
    if stage.get("output_schema"):
        issues += validate_table_schema(stage["output_schema"], f"`{sid}` output_schema")

    # Type-specific blocks
    if stype == "input_data":
        kind = block.get("kind")
        if kind is not None and kind not in CONNECTOR_KINDS:
            issues.append(f"`{sid}`: unknown connector kind `{kind}`")
        fmt = (block.get("params") or {}).get("format")
        if fmt is not None and fmt not in FILE_FORMATS and kind == "file":
            issues.append(f"`{sid}`: unknown file format `{fmt}`")
    elif stype == "python_transform":
        kind = block.get("kind")
        if kind not in FUNCTION_KINDS:
            issues.append(f"`{sid}`: function.kind must be one of {sorted(FUNCTION_KINDS)}")
        if kind == "module" and not block.get("module"):
            issues.append(f"`{sid}`: function.kind=module needs `module`")
        if kind == "inline" and not block.get("code"):
            issues.append(f"`{sid}`: function.kind=inline needs `code`")
    elif stype == "join":
        jt = block.get("type", "inner")
        if jt not in JOIN_TYPES:
            issues.append(f"`{sid}`: join.type `{jt}` not in {sorted(JOIN_TYPES)}")
        if not (block.get("keys") or block.get("on")):
            issues.append(f"`{sid}`: join needs `keys`")
    elif stype == "aggregate":
        for a in block.get("aggregations") or []:
            f = a.get("formula")
            if f not in AGG_FORMULAS:
                issues.append(f"`{sid}`: unknown aggregation formula `{f}`")
            if f in WEIGHTED_FORMULAS and not (a.get("value_column") and a.get("weight_column")):
                issues.append(f"`{sid}`: `{f}` needs value_column + weight_column")
    elif stype == "human_review_queue":
        if not block.get("hash_columns"):
            pk = (inputs[0].get("schema") or {}).get("primary_key") if inputs and isinstance(inputs[0], dict) else None
            if not pk:
                issues.append(f"`{sid}`: queue needs `hash_columns` or an upstream primary_key")
    elif stype == "llm_transform":
        tools = block.get("tools")
        if tools is not None and not (isinstance(tools, list) and all(isinstance(t, str) for t in tools)):
            issues.append(f"`{sid}`: llm.tools must be a list of tool-name strings")

    # Universal: limit
    if "limit" in stage and not isinstance(stage["limit"], int):
        issues.append(f"`{sid}`: `limit` must be an int")

    return issues


def validate_dag(stages: list[dict[str, Any]]) -> list[str]:
    """Cross-stage validation: unique ids, inputs resolve, acyclic."""
    issues: list[str] = []
    ids: list[str] = [s.get("id") for s in stages if s.get("id")]
    dupes = {i for i in ids if ids.count(i) > 1}
    for d in sorted(dupes):
        issues.append(f"duplicate stage id `{d}`")
    id_set = set(ids)

    edges: dict[str, list[str]] = {}
    for s in stages:
        sid = s.get("id")
        deps = []
        for inp in s.get("inputs") or []:
            iid = inp.get("id") if isinstance(inp, dict) else inp
            if iid and iid not in id_set:
                issues.append(f"`{sid}`: input `{iid}` references no stage")
            elif iid:
                deps.append(iid)
        edges[sid] = deps

    # cycle detection (DFS)
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {sid: WHITE for sid in edges}

    def visit(n: str, path: list[str]) -> None:
        color[n] = GRAY
        for m in edges.get(n, []):
            if color.get(m) == GRAY:
                issues.append(f"cycle: {' -> '.join(path + [n, m])}")
            elif color.get(m) == WHITE:
                visit(m, path + [n])
        color[n] = BLACK

    for sid in edges:
        if color[sid] == WHITE:
            visit(sid, [])
    return issues


def validate_methodology(stages: list[dict[str, Any]]) -> list[str]:
    """Validate every stage + the DAG as a whole. [] means a clean compile."""
    issues: list[str] = []
    for s in stages:
        issues += validate_stage(s)
    issues += validate_dag(stages)
    return issues


__all__ = [
    "SCALAR_COLUMN_TYPES", "NUMERIC_COLUMN_TYPES", "is_valid_column_type",
    "CONNECTOR_KINDS", "IMPLEMENTED_CONNECTOR_KINDS", "FILE_FORMATS",
    "AGG_FORMULAS", "JOIN_TYPES", "FUNCTION_KINDS", "PUBLISH_FORMATS",
    "UNIVERSAL_KEYS", "NODE_TYPES", "NODE_TYPE_NAMES",
    "SCHEMA_KINDS", "NAMED_SCHEMA_KEYS",
    "validate_table_schema", "validate_named_schema", "validate_schema_library",
    "build_ground_truth_schema", "validate_eval_spec",
    "validate_stage", "validate_dag", "validate_methodology",
]

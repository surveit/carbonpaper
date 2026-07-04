"""
node_review.py — node-level APPROVAL / BELIEF state for a methodology DAG.

This lifts the existing row-decision pattern (a reviewer accepting/rejecting a
flagged *data row*, keyed by a content hash) up one level: here a reviewer
accepts/rejects how a *DAG node is modeled*. The unit of belief is one compiled
stage spec; its identity is a content hash of the spec. Editing a node's knobs
changes the hash, so a prior approval no longer matches and the node auto-drops
to "edited_stale" — the staleness mechanic falls out of the hash for free, no
separate dirty-flag to keep in sync.

Two reviews, deliberately distinct:
  - NODE review (this module) = "do we trust how this step is modeled?" — colors
    the DAG, does NOT halt a run.
  - ROW review (app/runtime/stages/human_review_queue.py + app/main.py decisions
    store) = "is this run's data right?" — the human_review_queue, which DOES
    halt a run.

Dependency rule (mirrors app/models' discipline): this module imports NOTHING
from app.runtime or app.compiler. It is pure stdlib + yaml + pandas, so it stays
a trustworthy, side-effect-light interface that both the routes layer and the
versioning layer can lean on.

──────────────────────────────────────────────────────────────────────────────
CANONICAL-HASH INVARIANT (the one correctness rule that must not rot)

The content hash is computed over the LOADED stage dict, never the file text, so
whitespace / comment / key-reordering edits keep a node's approval, while any
semantic change (a model, a temperature, a column name, a prompt) drops it.

For that to hold, the canonical form must strip every key the *loader* injects
that is not part of the spec. Today the loader (app/main.py load_stages and
app/runtime/runner._load_stages) injects exactly:

    _filename  — the source YAML file name
    _order     — the numeric filename prefix
    _error     — set when a stage failed to parse

These are listed in CANONICAL_IGNORE_KEYS below. **If a future loader injects a
new bookkeeping key, it MUST be added to CANONICAL_IGNORE_KEYS** — otherwise that
key leaks into the hash and a cosmetic reload silently invalidates every prior
approval. This set is the single point of truth for that contract.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd

# Loader-injected bookkeeping keys that are NOT part of the stage spec and must
# be excluded from the canonical form before hashing. See the module docstring:
# this set is the invariant — extend it whenever a loader gains a new injected
# key, or cosmetic reloads will break approvals.
CANONICAL_IGNORE_KEYS: set[str] = {"_filename", "_order", "_error"}

# Columns of the node-decision store, in order. Mirrors the row-decisions store
# in app/main.py (content_hash + decision + reviewer + reviewed_at) plus the
# node-specific stage_id / dag_version provenance and a free-text note.
NODE_DECISION_COLUMNS: list[str] = [
    "stage_id", "content_hash", "decision",
    "reviewer", "reviewed_at", "dag_version", "note",
]

# The decision verbs persisted in the store and the approval state each maps to.
# (The HTTP route validates the inbound verb; this is the storage-side vocab.)
DECISION_APPROVE = "approve"
DECISION_REJECT = "reject"


def canonical_node_spec(stage: dict[str, Any]) -> dict[str, Any]:
    """Return the stage dict stripped of loader-injected bookkeeping keys
    (CANONICAL_IGNORE_KEYS), so two loads of the same spec — regardless of which
    file/whitespace/comment they came from — produce an identical mapping.

    Shallow strip is correct: the ignore-set keys are only ever injected at the
    top level by the loaders (see module docstring)."""
    return {k: v for k, v in stage.items() if k not in CANONICAL_IGNORE_KEYS}


def node_content_hash(stage: dict[str, Any]) -> str:
    """Stable sha1 (first 16 hex chars) of the canonical stage spec.

    Hashes the LOADED dict, not file text: json.dumps with sort_keys=True makes
    key order irrelevant, the tight separators drop incidental whitespace, and
    default=str lets any non-JSON-native scalar (e.g. a date) serialize. Mirrors
    app.runtime.stages.human_review_queue._content_hash (sha1 hexdigest, 16
    chars) so the node store and the row store share one hashing convention."""
    canonical = canonical_node_spec(stage)
    payload = json.dumps(
        canonical, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def node_decisions_path(methodology_dir: Path) -> Path:
    """examples/<methodology>/node_decisions.parquet — the single, version-
    independent store of node approvals. Keyed by (stage_id, content_hash) so an
    unchanged node carries its approval across versions automatically."""
    return Path(methodology_dir) / "node_decisions.parquet"


def load_node_decisions(methodology_dir: Path) -> pd.DataFrame:
    """Load the node-decision store, or an empty, correctly-typed frame when none
    exists yet (mirrors _load_decisions_df in app/main.py)."""
    p = node_decisions_path(methodology_dir)
    if not p.exists():
        return pd.DataFrame(columns=NODE_DECISION_COLUMNS)
    return pd.read_parquet(p)


def record_node_decision(
    methodology_dir: Path,
    *,
    stage_id: str,
    content_hash: str,
    decision: str,
    reviewer: str,
    dag_version: str | None = None,
    note: str | None = None,
    reviewed_at: str | None = None,
) -> pd.DataFrame:
    """Upsert one node decision and persist it. Drops any prior row matching the
    SAME (stage_id, content_hash) then appends — the exact idiom of queue_decide
    in app/main.py, extended to the two-column key. Returns the new frame.

    Note we key the upsert on (stage_id, content_hash), NOT content_hash alone:
    a content hash is only unique within a stage_id, and keeping the pair lets
    superseded hashes of the same stage remain in the store as history (which is
    what edited_stale detection reads)."""
    from datetime import datetime

    if reviewed_at is None:
        reviewed_at = datetime.now().isoformat(timespec="seconds")

    df = load_node_decisions(methodology_dir)
    mask = (df["stage_id"] == stage_id) & (df["content_hash"] == content_hash)
    df = df[~mask]
    new_row = {
        "stage_id": stage_id,
        "content_hash": content_hash,
        "decision": decision,
        "reviewer": reviewer,
        "reviewed_at": reviewed_at,
        "dag_version": dag_version,
        "note": note,
    }
    df = pd.concat([df, pd.DataFrame([new_row], columns=NODE_DECISION_COLUMNS)],
                   ignore_index=True)
    df.to_parquet(node_decisions_path(methodology_dir), index=False)
    return df


def _latest_decision_row(rows: pd.DataFrame) -> dict[str, Any] | None:
    """Most-recent decision row by reviewed_at (ISO strings sort lexically, so a
    plain max is correct). Returns None for an empty frame."""
    if rows.empty:
        return None
    idx = rows["reviewed_at"].astype(str).idxmax()
    # Column labels are Hashable; this store's are all strings — coerce at the
    # boundary so the returned mapping honors the dict[str, Any] contract.
    return {str(k): v for k, v in rows.loc[idx].to_dict().items()}


def approval_state_for(stage: dict[str, Any], df: pd.DataFrame) -> dict[str, Any]:
    """Compute the approval state of one stage against the decision store.

    state ∈ {approved, rejected, unreviewed, edited_stale}:
      - approved      : the CURRENT hash's latest decision is approve.
      - rejected      : the CURRENT hash's latest decision is reject.
      - edited_stale  : the current hash has no decision, but SOME prior hash of
                        this stage_id was approved — i.e. the node was approved
                        then edited; the green no longer applies, surface amber.
      - unreviewed    : the current hash has no decision and no prior approval.

    Returns {state, current_hash, matched_decision} where matched_decision is the
    decision row that determined the state (None for a clean unreviewed node)."""
    current_hash = node_content_hash(stage)
    sid = stage.get("id")

    if df is None or df.empty:
        return {"state": "unreviewed", "current_hash": current_hash,
                "matched_decision": None}

    stage_rows = df[df["stage_id"] == sid]
    current_rows = stage_rows[stage_rows["content_hash"] == current_hash]
    matched = _latest_decision_row(current_rows)
    if matched is not None:
        if matched["decision"] == DECISION_APPROVE:
            state = "approved"
        elif matched["decision"] == DECISION_REJECT:
            state = "rejected"
        else:
            # Any other persisted verb (e.g. needs_changes) is a non-approval on
            # the current hash → treat as unreviewed-equivalent for coloring but
            # keep the matched row so the panel can show what was recorded.
            state = "unreviewed"
        return {"state": state, "current_hash": current_hash,
                "matched_decision": matched}

    # No decision on the current hash. Did a PRIOR hash of this stage get approved?
    prior_approved = stage_rows[stage_rows["decision"] == DECISION_APPROVE]
    if not prior_approved.empty:
        return {"state": "edited_stale", "current_hash": current_hash,
                "matched_decision": _latest_decision_row(prior_approved)}

    return {"state": "unreviewed", "current_hash": current_hash,
            "matched_decision": None}


def coverage_for(stages: list[dict[str, Any]], df: pd.DataFrame) -> dict[str, Any]:
    """Aggregate approval coverage over a list of stages.

    Returns {approved, rejected, edited_stale, unreviewed, total, approved_pct}.
    approved_pct is over total stages (0 when there are no stages — no division
    by zero, no fabricated denominator)."""
    counts = {"approved": 0, "rejected": 0, "edited_stale": 0, "unreviewed": 0}
    for stage in stages:
        st = approval_state_for(stage, df)["state"]
        counts[st] = counts.get(st, 0) + 1
    total = len(stages)
    approved_pct = round(100.0 * counts["approved"] / total, 1) if total else 0.0
    return {
        "approved": counts["approved"],
        "rejected": counts["rejected"],
        "edited_stale": counts["edited_stale"],
        "unreviewed": counts["unreviewed"],
        "total": total,
        "approved_pct": approved_pct,
    }


# ──────────────────────────────────────────────────────────────────────────────
# DATA-MODEL APPROVAL GATE — the whole schema library as ONE synthetic node.
#
# PR#12 gates the DAG build on a human approving the DATA MODEL (the set of named
# schemas) as a whole — one approval for the whole library, not per-table. Rather
# than a new store, we reuse the node-decision store VERBATIM by treating the
# schema library as a single synthetic node under the sentinel stage_id below.
# Its "content" is a hash of all the schemas (name-sorted), so editing ANY schema
# changes the library hash and the prior approval auto-drops to edited_stale — the
# exact staleness mechanic of a real node, for free.
# ──────────────────────────────────────────────────────────────────────────────

# Sentinel stage_id the schema-library approval is recorded under. Underscore
# prefix keeps it out of the snake_case namespace of real stage ids, so it can
# never collide with a compiled stage.
SCHEMA_LIBRARY_STAGE_ID = "_schema_library"


def schema_library_content_hash(schemas: list[dict[str, Any]]) -> str:
    """Stable hash identifying a whole DATA MODEL (set of named schemas) as one
    unit, so an approval can be keyed to the exact library that was approved.

    Wraps the schemas (SORTED BY NAME, so file order / load order is irrelevant)
    in a synthetic `{"_type":"schema_library","schemas":[...]}` node and hashes it
    with the SAME node_content_hash used for real stages — one hashing convention
    across the whole approval layer. Reindenting / reordering keys inside a schema
    leaves the hash unchanged (json.dumps sort_keys); a column/name/type change
    flips it, which is what drops the approval to edited_stale."""
    ordered = sorted(
        schemas, key=lambda s: s.get("name") or "" if isinstance(s, dict) else ""
    )
    return node_content_hash({"_type": "schema_library", "schemas": ordered})


def approve_schema_library(
    methodology_dir: Path,
    *,
    content_hash: str,
    reviewer: str = "local",
    note: str | None = None,
) -> pd.DataFrame:
    """Record human approval of the whole data model, keyed to `content_hash`
    (the schema_library_content_hash the caller computed from the live schemas).

    A thin wrapper over record_node_decision under SCHEMA_LIBRARY_STAGE_ID — same
    store, same upsert semantics as approving a real node. Editing any schema then
    changes the library hash, so this approval no longer matches the live hash and
    data_model_state() reports edited_stale (re-locking the DAG build)."""
    return record_node_decision(
        methodology_dir,
        stage_id=SCHEMA_LIBRARY_STAGE_ID,
        content_hash=content_hash,
        decision=DECISION_APPROVE,
        reviewer=reviewer,
        note=note,
    )


def data_model_state(
    methodology_dir: Path, schemas: list[dict[str, Any]]
) -> dict[str, Any]:
    """Approval state of the DATA MODEL as it currently sits on disk.

    `schemas` are the LIVE schemas (loaded from methodology_dir/schemas by the
    caller). The CURRENT hash is computed from them via schema_library_content_hash
    — so editing any schema changes the hash, a prior approval no longer matches,
    and the state drops to edited_stale on its own (no dirty-flag to maintain).

    This applies approval_state_for's EXACT decision logic to the
    SCHEMA_LIBRARY_STAGE_ID rows, but against the library hash rather than letting
    approval_state_for recompute a hash from a node dict: a synthetic node carrying
    {id: _schema_library} would hash differently from the library payload (id is
    not a canonical-ignore key, by design for real nodes), so the recomputed hash
    would never match a stored approval. Computing the hash here keeps the gate
    honest.

    Returns {state, current_hash} where state ∈ {approved, unreviewed,
    edited_stale}. The schema gate only ever records `approve`, so `rejected` does
    not arise here."""
    current_hash = schema_library_content_hash(schemas)
    df = load_node_decisions(methodology_dir)

    if df is None or df.empty:
        return {"state": "unreviewed", "current_hash": current_hash}

    lib_rows = df[df["stage_id"] == SCHEMA_LIBRARY_STAGE_ID]
    current_rows = lib_rows[lib_rows["content_hash"] == current_hash]
    matched = _latest_decision_row(current_rows)
    if matched is not None:
        state = "approved" if matched["decision"] == DECISION_APPROVE else "unreviewed"
        return {"state": state, "current_hash": current_hash}

    # No decision on the current hash. Did a PRIOR library hash get approved?
    # (approved-then-edited → amber, re-locking the DAG build.)
    prior_approved = lib_rows[lib_rows["decision"] == DECISION_APPROVE]
    if not prior_approved.empty:
        return {"state": "edited_stale", "current_hash": current_hash}

    return {"state": "unreviewed", "current_hash": current_hash}


__all__ = [
    "CANONICAL_IGNORE_KEYS",
    "NODE_DECISION_COLUMNS",
    "SCHEMA_LIBRARY_STAGE_ID",
    "canonical_node_spec",
    "node_content_hash",
    "node_decisions_path",
    "load_node_decisions",
    "record_node_decision",
    "approval_state_for",
    "coverage_for",
    "schema_library_content_hash",
    "approve_schema_library",
    "data_model_state",
]

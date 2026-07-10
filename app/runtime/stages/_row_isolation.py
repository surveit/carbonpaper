"""Per-row error isolation for the per-row stage handlers.

A stage whose handler maps a function over input rows (`python_row_function`,
`llm_transform`) must not lose the whole stage — and every good row's output —
to a single row that raises. These helpers let such a handler ISOLATE a per-row
failure: the failing row is dropped from the schema-conforming, user-facing
output but recorded — 1:1 with its input position — in a runtime-internal shadow
the runner persists next to `outputs/` as `errors/<stage_id>.jsonl`.

Nothing is silently dropped (repo cardinal rule): a failed row is a visible,
errored entry in the shadow, and the run surfaces a non-zero error count. This is
per-row loudness instead of whole-stage failure — and because the shadow keeps
the input's row positions, positional/"show-your-work" tracing still works even
though the user-facing table has M < N rows.

Only handlers that genuinely process one row at a time can isolate a single
row's failure; whole-frame handlers (`python_frame_function`, `join`,
`aggregate`) reshape the frame, so a failure there is genuinely whole-stage and
keeps the runner's existing whole-stage error path.

A per-row ``outcome`` (the shadow's row schema) is::

    {"input_row": int,              # 0-based position in the stage's input
     "status": "ok" | "error",
     "output_rows": int,            # user-facing rows this input row produced
     "error": {"type", "message", "traceback"} | None}
"""

from __future__ import annotations

import traceback
from typing import Any, Callable


def ok_outcome(input_row: int, output_rows: int = 1) -> dict[str, Any]:
    """A successful input row's shadow entry."""
    return {
        "input_row": input_row,
        "status": "ok",
        "output_rows": int(output_rows),
        "error": None,
    }


def error_outcome(
    input_row: int,
    message: str,
    *,
    error_type: str = "RowError",
    tb: str | None = None,
) -> dict[str, Any]:
    """A failed input row's shadow entry. `message`/`error_type`/`tb` describe the
    failure honestly — the row produced no user-facing output (`output_rows: 0`)."""
    return {
        "input_row": input_row,
        "status": "error",
        "output_rows": 0,
        "error": {"type": error_type, "message": message, "traceback": tb},
    }


def map_rows_isolated(
    records: list[dict[str, Any]],
    fn: Callable[[dict[str, Any]], Any],
    *,
    post: Callable[[int, dict[str, Any], Any], None] | None = None,
) -> tuple[list[Any], list[dict[str, Any]]]:
    """Map `fn` over `records`, isolating per-row failures.

    Returns ``(out_rows, outcomes)``. `out_rows` holds only the successful rows'
    results, in input order — the schema-conforming, user-facing output.
    `outcomes` is 1:1 with `records`: one entry per input row (the shadow). A row
    whose `fn` raises is excluded from `out_rows` and recorded as an error in
    `outcomes`; it is never silently dropped.

    `post`, if given, runs on each successful result OUTSIDE the isolation guard —
    it is the runtime's own post-condition on `fn`'s return value (e.g. "must be a
    dict"). Because a violated post-condition is a systemic authoring bug that
    would recur for every row, `post` is allowed to raise and fail the whole
    stage rather than being isolated per row.
    """
    out_rows: list[Any] = []
    outcomes: list[dict[str, Any]] = []
    for i, record in enumerate(records):
        try:
            result = fn(record)
        except Exception as exc:  # noqa: BLE001 — per-row isolation: authored row
            # code (a python_row_function body, a per-row LLM call) can raise ANY
            # exception, and the whole point of this feature is to record that one
            # row's failure in the shadow (below) and keep processing the rest
            # rather than let it abort the stage. Surfaced durably, never swallowed.
            outcomes.append(
                error_outcome(
                    i,
                    str(exc),
                    error_type=type(exc).__name__,
                    tb=traceback.format_exc(limit=8),
                )
            )
            continue
        if post is not None:
            post(i, record, result)  # may raise → whole-stage (systemic), not isolated
        out_rows.append(result)
        outcomes.append(ok_outcome(i))
    return out_rows, outcomes


def record_row_outcomes(
    ctx: dict[str, Any] | None, stage_id: str, outcomes: list[dict[str, Any]]
) -> None:
    """Stash a stage's per-row `outcomes` on the run context so the runner can
    persist the shadow next to `outputs/` and fold the error count into the
    manifest. Keyed by stage id; a no-op when `ctx` is None (defensive)."""
    if ctx is None:
        return
    ctx.setdefault("row_errors", {})[stage_id] = outcomes

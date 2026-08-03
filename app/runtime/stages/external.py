"""The external stage handler: one subprocess per input row — the row in as JSON
on stdin, one JSON row back on stdout.
"""
from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING

import pandas as pd

from app.core.frames import collapse_null_forms
from app.models import Stage
from app.models.stages.external import ExternalConfig, ExternalStage

from ..run_log import EXTERNAL_STDERR, LEVEL_DETAIL, RunLog
from .execution import Row, RowMapper, narrow_stage

if TYPE_CHECKING:  # the factory signature names it; only `run_log` is read here
    from ..context import RunContext

# How much of a failed process's output an error message carries. Enough to see
# what came back instead of a row; the whole of it is in the run log.
_EXCERPT_CHARS = 500


def make_external_row_mapper(
    stage: Stage, ctx: "RunContext", src: pd.DataFrame
) -> RowMapper:
    """One process per row: nothing crosses rows, so row independence is structural."""
    # A process per row rather than a long-lived one: the timeout is then a kill,
    # cleanup is the child exiting, and no state — a browser, a session, a lock —
    # can survive from one row into the next.
    external = narrow_stage(stage, ExternalStage).external

    def map_row(row: Row, index: int) -> Row:
        return _run_row_in_a_subprocess(stage.id, external, row, index, ctx.run_log)

    return map_row


def _run_row_in_a_subprocess(
    stage_id: str, external: ExternalConfig, row: Row, index: int, log: RunLog | None
) -> Row:
    finished = _spawn(stage_id, external, row, index)
    _log_child_stderr(log, stage_id, index, finished.stderr)
    if finished.returncode != 0:
        raise RuntimeError(
            f"external stage {stage_id}, row {index}: {_argv(external)} exited "
            f"{finished.returncode}; stderr: {_excerpt(finished.stderr)}"
        )
    return _parse_result_row(stage_id, index, finished.stdout)


def _spawn(
    stage_id: str, external: ExternalConfig, row: Row, index: int
) -> subprocess.CompletedProcess[str]:
    """Run the command over one row. Raises rather than returning a partial result."""
    # No shell: `command` is argv, so nothing is word-split or expanded, and the
    # row travels on stdin instead of inside an argument. `input=` writes it and
    # closes stdin, which is what tells the program its one row is complete.
    # `timeout=` is the enforcement: subprocess.run kills the child and reaps it
    # before the exception escapes, so a hung program leaves nothing behind.
    try:
        return subprocess.run(
            external.command,
            input=json.dumps(row, default=_json_safe),
            capture_output=True,
            text=True,
            timeout=external.timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"external stage {stage_id}, row {index}: {_argv(external)} exceeded "
            f"timeout_seconds={external.timeout_seconds} and was killed"
        ) from exc
    except OSError as exc:
        raise RuntimeError(
            f"external stage {stage_id}, row {index}: {_argv(external)} could not be "
            f"started — {exc}"
        ) from exc


def _parse_result_row(stage_id: str, index: int, stdout: str) -> Row:
    """The one JSON object the program wrote, or a raised error naming stage and row."""
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"external stage {stage_id}, row {index}: stdout is not one JSON object "
            f"({exc}); it carried: {_excerpt(stdout)}"
        ) from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(
            f"external stage {stage_id}, row {index}: stdout is a JSON "
            f"{type(parsed).__name__}, not one object; a row is an object of "
            f"column → value"
        )
    return parsed


def _log_child_stderr(log: RunLog | None, stage_id: str, index: int, stderr: str) -> None:
    """Route the child's stderr into the run log, so a failed capture is diagnosable."""
    if log is None or not stderr.strip():
        return
    log.emit({
        "kind": EXTERNAL_STDERR, "stage": stage_id, "row": index,
        "level": LEVEL_DETAIL, "text": stderr,
    })


def _json_safe(value: object) -> object:
    """Serialise a pandas cell type plain JSON cannot take (numpy scalar, Timestamp)."""
    collapsed = collapse_null_forms(value)
    if collapsed is None:
        return None
    item = getattr(collapsed, "item", None)
    return item() if callable(item) else str(collapsed)


def _argv(external: ExternalConfig) -> str:
    return " ".join(external.command)


def _excerpt(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return "(empty)"
    return stripped if len(stripped) <= _EXCERPT_CHARS else stripped[:_EXCERPT_CHARS] + "…"

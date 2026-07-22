"""The authoring loop: seed a corpus, smoke-run a sliced sample, read the
outcome, then launch the full run — the services-side logic the MCP tools in
`app.mcp.server` wrap thinly.

Runs are driven only through the `RunTool` seam (`app.services.run_tool`), never
by importing the runner: `get_run_tool()` is the module-level provider (a
`StubRunTool` by default, overridable in tests via `set_run_tool`). So this
module honours the `app.services ↛ app.runtime` contract while still starting
and reading runs.

Corpus provenance: seeds and the smoke-run seed check are both keyed to the
corpus *as executed* — the first connector stage's output frame of a run on disk
(`<project>/runs/<run_id>/outputs/<stage_id>.parquet`, csv fallback). Recording
uses the latest run's connector output; reading a run's result uses that run's.
Both hash rows the same way, so a seed recorded off one run is comparable to a
later run's connector output.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
from pandas import DataFrame

from app.core.errors import RunNotFoundError, RunToolUnavailableError
from app.models.seed_rows import SeedOutcome, SeedRow
from app.services import seed_rows
from app.services import versioning
from app.services.run_tool import RunTool, StubRunTool, read_run_manifest

_run_tool: RunTool = StubRunTool()


def get_run_tool() -> RunTool:
    """The run tool the authoring loop drives runs through — a `StubRunTool` by
    default (starting runs fails loudly), swapped for a real or fake one via
    `set_run_tool`."""
    return _run_tool


def set_run_tool(tool: RunTool) -> None:
    """Override the module's run tool (a real driver in production wiring, a fake
    in tests)."""
    global _run_tool
    _run_tool = tool


def record_seeds(project_dir: Path, seeds_json: str, key_column: str) -> dict[str, object]:
    """Record user-asserted must/must-not-catch corpus rows, hashing each against
    the latest run's input corpus (its first connector stage's output frame), so
    a later edit to that corpus row reads as stale.

    `seeds_json` is a JSON array of `{"row_key", "outcome", "note"?}` objects,
    `outcome` being `must_catch` / `must_not_catch`. Fails loudly if the project
    has no run to key the corpus to, or if a seeded `row_key` is absent from the
    corpus key column."""
    run_id = _latest_run_id(project_dir)
    corpus = _first_connector_output(project_dir, run_id)
    seeds = _parse_seeds(seeds_json)
    seed_rows.record_seeds(project_dir, seeds, corpus, key_column)
    return {"recorded": len(seeds), "key_column": key_column, "corpus_run_id": run_id}


def smoke_run(
    project_dir: Path, version_id: str, limit: int, offset: int = 0
) -> dict[str, object]:
    """Start a sampled run of `version_id`: cap every connector stage to `limit`
    rows (after dropping `offset`), so the loop can exercise the whole pipeline
    cheaply before committing to the full corpus.

    Returns `{ok: True, run_id, run_url, limits, offsets}` on start, or
    `{ok: False, error}` when the run tool cannot start runs yet — never a
    fabricated run id."""
    connector_ids = _connector_stage_ids(project_dir, version_id)
    limits = {stage_id: limit for stage_id in connector_ids}
    offsets = {stage_id: offset for stage_id in connector_ids}
    result = _start_run(project_dir, version_id, limits=limits, offsets=offsets)
    if result["ok"]:
        result["limits"] = limits
        result["offsets"] = offsets
    return result


def read_run_result(
    project_dir: Path,
    run_id: str,
    positive_column: str = "",
    positive_stage_id: str = "",
) -> dict[str, object]:
    """Read a finished run's outcome for the authoring loop: overall status,
    per-stage status/row-count/model-usage, run-total usage, the web run-page
    URL, and — when a positive stage/column is named and seeds exist — which
    seeds the run failed.

    `positive_stage_id` names the stage whose output lists the flagged corpus
    rows, `positive_column` the column in it holding the corpus key. The flagged
    keys are compared against the recorded seeds (`find_failing_seeds`), with
    staleness checked against the run's own input corpus. Without both fields, or
    with no seeds recorded, `failing_seeds` is `[]` and `seeds_checked` is False.
    `staleness_checked` is False whenever the staleness comparison itself did not
    run — including when `positive_column` is absent from the run's corpus — so a
    seed that could not be checked for staleness never reads as silently clean."""
    manifest = read_run_manifest(project_dir, run_id)
    stages = [
        {
            "stage_id": stage.stage_id,
            "status": stage.status,
            "row_count": stage.row_count,
            "llm_usage": stage.llm_usage.model_dump() if stage.llm_usage else None,
        }
        for stage in manifest.stages
    ]
    failing_seeds, seeds_checked, staleness_checked = _grade_seeds(
        project_dir, run_id, positive_column, positive_stage_id
    )
    return {
        "status": manifest.status,
        "stages": stages,
        "total_usage": manifest.total_usage().model_dump(),
        "run_url": f"/project/{Path(project_dir).name}/runs/{run_id}",
        "failing_seeds": failing_seeds,
        "seeds_checked": seeds_checked,
        "staleness_checked": staleness_checked,
    }


def start_full_run(project_dir: Path, version_id: str) -> dict[str, object]:
    """Start the full, unsliced run of `version_id` — no per-connector limits or
    offsets. Intended only after a human has reviewed the smoke run's output; the
    loop does not enforce that gate. Returns `{ok: True, run_id, run_url}` or
    `{ok: False, error}` when the run tool cannot start runs yet."""
    return _start_run(project_dir, version_id, limits=None, offsets=None)


def _start_run(
    project_dir: Path,
    version_id: str,
    *,
    limits: dict[str, int] | None,
    offsets: dict[str, int] | None,
) -> dict[str, object]:
    """Drive the run tool, translating a not-yet-wired start into a structured
    `{ok: False, error}` rather than letting the exception escape — so the loop
    surfaces the seam's absence without fabricating a run id."""
    try:
        run_id = get_run_tool().start_run(
            project_dir, version_id=version_id, limits=limits, offsets=offsets
        )
    except RunToolUnavailableError as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "run_id": run_id,
        "run_url": f"/project/{Path(project_dir).name}/runs/{run_id}",
    }


def _grade_seeds(
    project_dir: Path, run_id: str, positive_column: str, positive_stage_id: str
) -> tuple[list[str], bool, bool]:
    """Grade the recorded seeds against this run, or report the check was not
    run. Returns (failing_seed_messages, seeds_checked, staleness_checked). The
    check runs only with both a positive stage/column and recorded seeds; the
    flagged keys are the distinct values of `positive_column` in the named
    stage's output, and staleness is checked against the run's first connector
    output under the same key column."""
    seeds = seed_rows.load_seeds(project_dir)
    if not (positive_column and positive_stage_id and seeds):
        return [], False, False
    positive_frame = _stage_output(project_dir, run_id, positive_stage_id)
    positive_keys = set(positive_frame[positive_column].astype(str))
    corpus = _first_connector_output(project_dir, run_id)
    stale_messages, staleness_checked = _stale_messages(seeds, corpus, positive_column)
    failing = seed_rows.find_failing_seeds(seeds, positive_keys, stale_messages)
    return failing, True, staleness_checked


def _stale_messages(
    seeds: list[SeedRow], corpus: DataFrame, key_column: str
) -> tuple[list[str], bool]:
    """Staleness against the run's own corpus, keyed by `key_column` (the same
    column the positive stage carries the corpus key in). If that column is not
    in the corpus frame the run cannot have keyed rows by it, so staleness is not
    checkable and no drift is asserted — signalled by a False second element
    rather than folding silently into an empty stale-messages list."""
    if key_column not in corpus.columns:
        return [], False
    return seed_rows.find_stale_seeds(seeds, corpus, key_column), True


def _connector_stage_ids(project_dir: Path, version_id: str) -> list[str]:
    """Ids of the version's connector stages (the sources a run slices), in the
    version's stage order."""
    stages = versioning.load_version_stages(project_dir, version_id)
    return [stage.id for stage in stages if stage.connector is not None]


def _first_connector_output(project_dir: Path, run_id: str) -> DataFrame:
    """The run's input corpus: the output frame of the FIRST connector stage that
    ran, identified from the run's pinned version. Fails loudly if the version
    lists no connector stage, or none of them produced an output in this run."""
    manifest = read_run_manifest(project_dir, run_id)
    connector_ids = set(_connector_stage_ids(project_dir, manifest.workflow_version))
    for stage in manifest.stages:
        if stage.stage_id in connector_ids:
            path = _output_path(project_dir, run_id, stage.stage_id)
            if path is not None:
                return _read_output_table(path)
    raise RunNotFoundError(
        f"run {run_id} has no connector stage output to use as the seed corpus"
    )


def _stage_output(project_dir: Path, run_id: str, stage_id: str) -> DataFrame:
    """One stage's output frame from a run, read off disk. Fails loudly if the
    stage produced no output file in this run."""
    path = _output_path(project_dir, run_id, stage_id)
    if path is None:
        raise RunNotFoundError(
            f"run {run_id} stage {stage_id!r} has no output frame on disk"
        )
    return _read_output_table(path)


def _output_path(project_dir: Path, run_id: str, stage_id: str) -> Path | None:
    """The on-disk output path for one stage of a run — parquet, or a csv
    fallback (the runner writes csv for frames parquet cannot serialise) — or
    None when neither exists."""
    outputs = Path(project_dir) / "runs" / run_id / "outputs"
    parquet = outputs / f"{stage_id}.parquet"
    if parquet.exists():
        return parquet
    csv = outputs / f"{stage_id}.csv"
    if csv.exists():
        return csv
    return None


def _read_output_table(path: Path) -> DataFrame:
    """Read a stage output frame (parquet or csv) by suffix."""
    if path.suffix == ".parquet":
        return pd.read_parquet(path)
    return pd.read_csv(path)


def _latest_run_id(project_dir: Path) -> str:
    """The newest run id for a project (run ids are strftime timestamps, so the
    lexical max is the most recent). Fails loudly if the project has no run —
    seeds must be keyed to a corpus that was actually executed."""
    runs_dir = Path(project_dir) / "runs"
    run_ids = sorted(
        child.name for child in runs_dir.iterdir() if child.is_dir()
    ) if runs_dir.is_dir() else []
    if not run_ids:
        raise RunNotFoundError(
            f"project {Path(project_dir).name} has no run to key seeds to — "
            "smoke-run the workflow before recording seeds"
        )
    return run_ids[-1]


def _parse_seeds(seeds_json: str) -> list[SeedRow]:
    """Parse the seeds JSON array into SeedRow objects. `row_content_hash` is
    left empty here and stamped from the live corpus by `seed_rows.record_seeds`
    before anything is persisted — no placeholder hash is ever written."""
    payload = json.loads(seeds_json)
    if not isinstance(payload, list):
        raise ValueError("seeds_json must be a JSON array of seed objects")
    seeds: list[SeedRow] = []
    for entry in payload:
        seeds.append(
            SeedRow(
                row_key=str(entry["row_key"]),
                outcome=SeedOutcome(entry["outcome"]),
                note=entry.get("note"),
                row_content_hash="",
            )
        )
    return seeds

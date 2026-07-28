"""Whole workflows run TWICE through the production run entry points.

Every other stage-cache test drives `handler.execute(...)` directly, so the path
the CLI and the web trigger actually take — `prepare_run` / `run_prepared` via
`execute_run`, over a published version, against the process-wide stores — was
never covered end to end.

The evidence is the stages' own authored code, not the cache's internals: every
stage appends a line to a probe file when its body runs, so a run that replays
leaves the probe untouched. A run that recomputes appends one line per row.
"""
from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path

import pandas as pd
from pandas.testing import assert_frame_equal

from app.runtime.runner import execute_run
from app.services import versioning

_ROWS = [{"name": "a", "val": 1}, {"name": "b", "val": 2}, {"name": "c", "val": 3}]

# One computing run's probe tally over `_ROWS`: a row-mapped stage's body runs
# once per row.
_EVERY_ROW_COMPUTED = Counter({"clean": 3, "flag": 3})
_NOTHING_COMPUTED: Counter[str] = Counter()


def _probe_call(probe: Path, tag: str) -> str:
    """Two lines of authored python that append `tag` to the probe file. The
    path is embedded as a literal, so it is part of the stage's definition
    fingerprint — constant within one test, which is what the cache keys on."""
    return (
        f"    with open({json.dumps(str(probe))}, 'a', encoding='utf-8') as handle:\n"
        f"        handle.write('{tag}\\n')\n"
    )


def _clean_code(probe: Path, *, edit: str = "") -> str:
    return (
        "def transform(row):\n"
        + _probe_call(probe, "clean")
        + "    return {**row, 'doubled': row['val'] * 2}\n"
        + edit
    )


def _flag_code(probe: Path) -> str:
    return (
        "def transform(row):\n"
        + _probe_call(probe, "flag")
        + "    return {**row, 'big': row['doubled'] > 3}\n"
    )


def _write_project(root: Path, *, clean_edit: str = "", flag_cache: bool = True) -> Path:
    """`load` (csv) -> `clean` -> `flag`, two row-mapped stages deep. Returns the
    probe file both stages append to."""
    probe = root / "probe.log"
    (root / "compiled").mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir(parents=True, exist_ok=True)
    pd.DataFrame(_ROWS).to_csv(root / "data" / "items.csv", index=False)
    _write_stage(root, "01_load", {
        "id": "load", "name": "Load items", "type": "input_data",
        "connector": {"kind": "file", "params": {
            "path": str(root / "data" / "items.csv"), "format": "csv"}},
    })
    _write_stage(root, "02_clean", {
        "id": "clean", "name": "Clean", "type": "python_row_function",
        "inputs": [{"id": "load"}],
        "function": {"kind": "inline", "code": _clean_code(probe, edit=clean_edit)},
    })
    _write_stage(root, "03_flag", {
        "id": "flag", "name": "Flag", "type": "python_row_function",
        "inputs": [{"id": "clean"}], "cache": flag_cache,
        "function": {"kind": "inline", "code": _flag_code(probe)},
    })
    return probe


def _append_input_row(root: Path, row: dict[str, object]) -> None:
    """Add a row to the csv `load` reads. `load` is a source stage — nothing
    caches it — so the next run simply sees the longer frame."""
    pd.DataFrame(_ROWS + [row]).to_csv(root / "data" / "items.csv", index=False)


def _write_stage(root: Path, filename: str, spec: dict[str, object]) -> None:
    (root / "compiled" / f"{filename}.json").write_text(
        json.dumps(spec), encoding="utf-8")


def _publish_a_version(root: Path) -> str:
    version = versioning.create_version_from_disk(
        root, message="cache e2e", reviewer="test")
    versioning.publish_version(root, version.version_id, reviewer="human")
    return version.version_id


def _run_and_read(
    project: Path, *, bust_cache: bool = False
) -> dict[str, pd.DataFrame]:
    """One whole run through the production entry point, and every stage's
    output frame read back off disk."""
    if (project / "runs").exists():
        time.sleep(1.05)  # run ids are second-resolution: one dir per run
    manifest = execute_run(project, repo_root=project, bust_cache=bust_cache)
    assert manifest["status"] == "ok", manifest
    run_dir = project / "runs" / manifest["run_id"]
    return {
        record["stage_id"]: pd.read_parquet(run_dir / record["output_path"])
        for record in manifest["stage_records"]
    }


def _invocations(probe: Path) -> Counter[str]:
    """How many times each stage's authored body ran, across every run so far."""
    if not probe.exists():
        return Counter()
    return Counter(probe.read_text(encoding="utf-8").split())


def _assert_same_outputs(
    first: dict[str, pd.DataFrame], second: dict[str, pd.DataFrame]
) -> None:
    assert sorted(first) == sorted(second)
    for stage_id, frame in first.items():
        assert_frame_equal(second[stage_id], frame, obj=stage_id)


def test_a_second_run_recomputes_nothing_and_reproduces_the_first_exactly(tmp_path):
    probe = _write_project(tmp_path)
    _publish_a_version(tmp_path)

    first = _run_and_read(tmp_path)
    assert _invocations(probe) == _EVERY_ROW_COMPUTED

    second = _run_and_read(tmp_path)
    assert _invocations(probe) == _EVERY_ROW_COMPUTED  # no body ran a second time
    _assert_same_outputs(first, second)


def test_bust_cache_recomputes_everything_and_leaves_the_cache_re_pinned(tmp_path):
    """Re-pinned, not merely stale. A busted run over rows the first run already
    pinned proves only that the READS were skipped — the run after it would hit
    the first run's entries either way. So the busted run is given a row NOTHING
    has ever pinned: the run after it replays that row only if the busted run
    recorded what it recomputed."""
    probe = _write_project(tmp_path)
    _publish_a_version(tmp_path)

    _run_and_read(tmp_path)
    assert _invocations(probe) == _EVERY_ROW_COMPUTED

    _append_input_row(tmp_path, {"name": "d", "val": 4})
    busted = _run_and_read(tmp_path, bust_cache=True)
    # Every row recomputed, including the three already pinned: reads skipped.
    assert _invocations(probe) == _EVERY_ROW_COMPUTED + Counter({"clean": 4, "flag": 4})

    after = _run_and_read(tmp_path)
    # Unchanged — so row "d", which only the busted run ever computed, was pinned
    # by it.
    assert _invocations(probe) == _EVERY_ROW_COMPUTED + Counter({"clean": 4, "flag": 4})
    _assert_same_outputs(busted, after)


def test_editing_one_stages_function_body_invalidates_that_stage_alone(tmp_path):
    """`clean`'s edit changes its definition fingerprint but not its output, so
    `flag` still sees the rows it was pinned against and replays."""
    probe = _write_project(tmp_path)
    _publish_a_version(tmp_path)
    first = _run_and_read(tmp_path)

    time.sleep(1.05)  # version ids are second-resolution
    _write_project(tmp_path, clean_edit="\n# a comment the cache must notice\n")
    _publish_a_version(tmp_path)

    edited = _run_and_read(tmp_path)
    assert _invocations(probe) == _EVERY_ROW_COMPUTED + Counter({"clean": 3})
    _assert_same_outputs(first, edited)


def test_a_stage_declaring_cache_false_recomputes_on_every_run(tmp_path):
    probe = _write_project(tmp_path, flag_cache=False)
    _publish_a_version(tmp_path)

    _run_and_read(tmp_path)
    _run_and_read(tmp_path)

    assert _invocations(probe) == _EVERY_ROW_COMPUTED + Counter({"flag": 3})


def test_a_first_run_of_a_fresh_project_replays_nothing(tmp_path):
    """The baseline the rest of this module reads against: with an empty cache
    every stage body runs, so a later tally that does NOT grow is evidence of a
    replay rather than of a probe that never fired."""
    probe = _write_project(tmp_path)
    assert _invocations(probe) == _NOTHING_COMPUTED
    _publish_a_version(tmp_path)
    _run_and_read(tmp_path)
    assert _invocations(probe) == _EVERY_ROW_COMPUTED

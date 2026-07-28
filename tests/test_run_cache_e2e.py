"""Whole workflows run twice through the production entry points, spanning both cache
grains - row-mapped and frame-shaped are intercepted by different code. Evidence is the
stages' own authored code: each appends a line to a probe file when its body runs, so a
replayed run leaves the probe untouched."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

import pandas as pd
from pandas.testing import assert_frame_equal

from app.runtime.runner import execute_run
from app.services import versioning

_ROWS = [{"name": "a", "val": 1}, {"name": "b", "val": 2}, {"name": "c", "val": 3}]

_LOADED = [{"name": "name", "type": "str"}, {"name": "val", "type": "int"}]
_CLEANED = [*_LOADED, {"name": "doubled", "type": "int"}]
_FLAGGED = [*_CLEANED, {"name": "big", "type": "bool"}]
_TOTALLED = [*_FLAGGED, {"name": "total", "type": "int"}]

# One fully-computing run's probe tally over `_ROWS`: a row-mapped stage's body
# runs once per row, a frame-shaped stage's once for the whole frame.
_EVERYTHING_COMPUTED = Counter({"clean": 3, "flag": 3, "totals": 1})
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


def _totals_code(probe: Path, *, edit: str = "") -> str:
    return (
        "def transform(df):\n"
        + _probe_call(probe, "totals")
        + "    return df.assign(total=df['doubled'].sum())\n"
        + edit
    )


def _write_project(
    root: Path, *, clean_edit: str = "", totals_edit: str = "", flag_cache: bool = True
) -> Path:
    """`load` (csv) -> `clean` -> `flag` -> `totals`: two row-mapped stages and
    then a frame-shaped one, so a run crosses both cache grains. Returns the
    probe file every stage appends to."""
    probe = root / "probe.log"
    (root / "compiled").mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir(parents=True, exist_ok=True)
    pd.DataFrame(_ROWS).to_csv(root / "data" / "items.csv", index=False)
    _write_stage(root, "01_load", {
        "id": "load", "name": "Load items", "type": "input_data",
        "connector": {"kind": "file", "params": {
            "path": str(root / "data" / "items.csv"), "format": "csv"}},
        "output_schema": {"columns": _LOADED},
    })
    _write_stage(root, "02_clean", {
        "id": "clean", "name": "Clean", "type": "python_row_function",
        "inputs": [{"id": "load", "schema": {"columns": _LOADED}}],
        "output_schema": {"columns": _CLEANED},
        "function": {"kind": "inline", "code": _clean_code(probe, edit=clean_edit)},
    })
    _write_stage(root, "03_flag", {
        "id": "flag", "name": "Flag", "type": "python_row_function",
        "inputs": [{"id": "clean", "schema": {"columns": _CLEANED}}], "cache": flag_cache,
        "output_schema": {"columns": _FLAGGED},
        "function": {"kind": "inline", "code": _flag_code(probe)},
    })
    _write_stage(root, "04_totals", {
        "id": "totals", "name": "Totals", "type": "python_frame_function",
        "inputs": [{"id": "flag", "schema": {"columns": _FLAGGED}}],
        "output_schema": {"columns": _TOTALLED},
        "function": {"kind": "inline", "code": _totals_code(probe, edit=totals_edit)},
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
    assert _invocations(probe) == _EVERYTHING_COMPUTED

    second = _run_and_read(tmp_path)
    assert _invocations(probe) == _EVERYTHING_COMPUTED  # no body ran a second time
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
    assert _invocations(probe) == _EVERYTHING_COMPUTED

    _append_input_row(tmp_path, {"name": "d", "val": 4})
    busted = _run_and_read(tmp_path, bust_cache=True)
    # Everything recomputed, including what was already pinned: reads skipped at
    # both grains.
    assert _invocations(probe) == _EVERYTHING_COMPUTED + Counter(
        {"clean": 4, "flag": 4, "totals": 1})

    after = _run_and_read(tmp_path)
    # Unchanged — so row "d" and the four-row frame, which only the busted run
    # ever computed, were pinned by it.
    assert _invocations(probe) == _EVERYTHING_COMPUTED + Counter(
        {"clean": 4, "flag": 4, "totals": 1})
    _assert_same_outputs(busted, after)


def test_editing_one_stages_function_body_invalidates_that_stage_alone(tmp_path):
    """`clean`'s edit changes its definition fingerprint but not its output, so
    `flag` still sees the rows it was pinned against and `totals` still sees the
    frame it was pinned against — both replay."""
    probe = _write_project(tmp_path)
    _publish_a_version(tmp_path)
    first = _run_and_read(tmp_path)

    time.sleep(1.05)  # version ids are second-resolution
    _write_project(tmp_path, clean_edit="\n# a comment the cache must notice\n")
    _publish_a_version(tmp_path)

    edited = _run_and_read(tmp_path)
    assert _invocations(probe) == _EVERYTHING_COMPUTED + Counter({"clean": 3})
    _assert_same_outputs(first, edited)


def test_editing_the_frame_stages_body_invalidates_only_the_frame_stage(tmp_path):
    """The other grain of the same property: the frame entry is keyed on the
    stage definition too, and the row stages upstream are untouched by its edit."""
    probe = _write_project(tmp_path)
    _publish_a_version(tmp_path)
    first = _run_and_read(tmp_path)
    _run_and_read(tmp_path)
    # An unedited `totals` replays — otherwise the tally below could not tell an
    # invalidation apart from a frame stage that simply always recomputes.
    assert _invocations(probe) == _EVERYTHING_COMPUTED

    time.sleep(1.05)  # version ids are second-resolution
    _write_project(tmp_path, totals_edit="\n# a comment the cache must notice\n")
    _publish_a_version(tmp_path)

    edited = _run_and_read(tmp_path)
    assert _invocations(probe) == _EVERYTHING_COMPUTED + Counter({"totals": 1})
    _assert_same_outputs(first, edited)


def test_one_new_input_row_recomputes_only_that_row_but_the_whole_frame(tmp_path):
    """What the two grains cost differently: a row stage replays the three rows
    it pinned and computes only the new one, while the frame stage's entry is
    keyed on its WHOLE input, so a single new row misses it entirely."""
    probe = _write_project(tmp_path)
    _publish_a_version(tmp_path)
    _run_and_read(tmp_path)
    _run_and_read(tmp_path)
    assert _invocations(probe) == _EVERYTHING_COMPUTED  # nothing recomputes yet

    _append_input_row(tmp_path, {"name": "d", "val": 4})
    _run_and_read(tmp_path)

    assert _invocations(probe) == _EVERYTHING_COMPUTED + Counter(
        {"clean": 1, "flag": 1, "totals": 1})


def test_a_stage_declaring_cache_false_recomputes_on_every_run(tmp_path):
    probe = _write_project(tmp_path, flag_cache=False)
    _publish_a_version(tmp_path)

    _run_and_read(tmp_path)
    _run_and_read(tmp_path)

    # `flag` re-rolled; `clean` above it and `totals` below it both replayed —
    # the opt-out is that stage's alone.
    assert _invocations(probe) == _EVERYTHING_COMPUTED + Counter({"flag": 3})


def test_the_cache_survives_a_process_restart_and_a_change_of_directory(tmp_path):
    """The one property no in-process test can observe: a payload pinned by a
    run that has EXITED is replayed by a later, unrelated process. Both grains
    at once — the row payloads live in the document store, the frame payload in
    a parquet under the frame store, so a tally that does not grow is evidence
    that both were read back off disk.

    The two runs are launched from DIFFERENT directories, neither of them the
    project, with only CW_DB_PATH set and the frames root left to its default:
    a frames root that resolved against the working directory rather than
    against the pinned database would send the two runs to different roots, and
    the second would miss the frame entry and recompute `totals`.

    Two real interpreter startups is the price; nothing cheaper distinguishes a
    durable store from a process-lifetime one."""
    from app.core.persistence import SqliteKvStore, configure_store

    db = tmp_path / "workspace" / "app.db"
    db.parent.mkdir()
    configure_store(SqliteKvStore(str(db)))  # the version must outlive this process too
    probe = _write_project(tmp_path)
    _publish_a_version(tmp_path)
    first_cwd, second_cwd = tmp_path / "launch_a", tmp_path / "launch_b"
    first_cwd.mkdir()
    second_cwd.mkdir()

    _run_in_a_fresh_process(tmp_path, db=db, cwd=first_cwd)
    assert _invocations(probe) == _EVERYTHING_COMPUTED

    time.sleep(1.05)  # run ids are second-resolution: one dir per run
    _run_in_a_fresh_process(tmp_path, db=db, cwd=second_cwd)

    assert _invocations(probe) == _EVERYTHING_COMPUTED  # every stage replayed


def _run_in_a_fresh_process(project: Path, *, db: Path, cwd: Path) -> None:
    """One whole run through the runner CLI in a process that has configured no
    store of its own — the faithful exercise of a restart, as
    tests/test_seed_cli.py is of a store-free process."""
    repo_root = Path(__file__).resolve().parents[1]
    env = {k: v for k, v in os.environ.items() if k != "CW_FRAMES_ROOT"}
    result = subprocess.run(
        [sys.executable, "-m", "app.runtime.runner", str(project)],
        cwd=cwd, env={**env, "PYTHONPATH": str(repo_root), "CW_DB_PATH": str(db)},
        capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        f"run crashed in a fresh process:\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


def test_a_first_run_of_a_fresh_project_replays_nothing(tmp_path):
    """The baseline the rest of this module reads against: with an empty cache
    every stage body runs, so a later tally that does NOT grow is evidence of a
    replay rather than of a probe that never fired."""
    probe = _write_project(tmp_path)
    assert _invocations(probe) == _NOTHING_COMPUTED
    _publish_a_version(tmp_path)
    _run_and_read(tmp_path)
    assert _invocations(probe) == _EVERYTHING_COMPUTED

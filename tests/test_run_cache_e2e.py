"""Whole workflows run twice for real; each stage body appends to a probe file when it runs."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pandas as pd
from pandas.testing import assert_frame_equal

from app.runtime.runner import execute_run
from app.services import project as project_service
from conftest import pinned_stages
from stage_seed import add_stage

_ROWS = [{"name": "a", "val": 1}, {"name": "b", "val": 2}, {"name": "c", "val": 3}]

_LOADED = [{"name": "name", "type": "str", "nullable": True}, {"name": "val", "type": "int", "nullable": True}]
_CLEANED = [*_LOADED, {"name": "doubled", "type": "int", "nullable": True}]
_FLAGGED = [*_CLEANED, {"name": "big", "type": "bool", "nullable": True}]
_TOTALLED = [*_FLAGGED, {"name": "total", "type": "int", "nullable": True}]

# probe count: row-mapped runs once per row; frame-shaped runs once for the frame.
_EVERYTHING_COMPUTED = Counter({"clean": 3, "flag": 3, "totals": 1})
# What every later run adds no matter what: a frame stage consults no cache.
_A_FURTHER_RUN = Counter({"totals": 1})
_NOTHING_COMPUTED: Counter[str] = Counter()


def _probe_call(probe: Path, tag: str) -> str:
    """The probe path is a literal in the code, so it is part of the stage's definition fingerprint."""
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


def _totals_code(probe: Path) -> str:
    return (
        "def transform(df):\n"
        + _probe_call(probe, "totals")
        + "    return df.assign(total=df['doubled'].sum())\n"
    )


def _write_project(
    root: Path, *, clean_edit: str = "", flag_cache: bool = True
) -> Path:
    probe = root / "probe.log"
    root.mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir(parents=True, exist_ok=True)
    pd.DataFrame(_ROWS).to_csv(root / "data" / "items.csv", index=False)
    _write_stage(root, "01_load", {
        "id": "load", "description": "Load items", "type": "input_data",
        "connector": {"kind": "file", "params": {
            "path": str(root / "data" / "items.csv"), "format": "csv"}},
        "signature": {"form": "replaces", "produces": _LOADED},
    })
    _write_stage(root, "02_clean", {
        "id": "clean", "description": "Clean", "type": "python_row_function",
        "inputs": [{"id": "load"}], "cache": True,
        "signature": {
            "form": "extends",
            "reads": [{"input": "load", "columns": _LOADED}],
            "adds": [{"name": "doubled", "type": "int", "nullable": True}],
        },
        "function": {"kind": "inline", "code": _clean_code(probe, edit=clean_edit)},
    })
    _write_stage(root, "03_flag", {
        "id": "flag", "description": "Flag", "type": "python_row_function",
        "inputs": [{"id": "clean"}], "cache": flag_cache,
        "signature": {
            "form": "extends",
            "reads": [{"input": "clean", "columns": _CLEANED}],
            "adds": [{"name": "big", "type": "bool", "nullable": True}],
        },
        "function": {"kind": "inline", "code": _flag_code(probe)},
    })
    _write_stage(root, "04_totals", {
        "id": "totals", "description": "Totals", "type": "python_frame_function",
        "inputs": [{"id": "flag"}],
        "signature": {
            "form": "replaces",
            "reads": [{"input": "flag", "columns": _FLAGGED}],
            "produces": _TOTALLED,
        },
        "function": {"kind": "inline", "code": _totals_code(probe)},
    })
    return probe


def _append_input_row(root: Path, row: dict[str, object]) -> None:
    pd.DataFrame(_ROWS + [row]).to_csv(root / "data" / "items.csv", index=False)


def _write_stage(root: Path, filename: str, spec: dict[str, object]) -> None:
    add_stage(root, spec)


def _publish_a_version(root: Path) -> str:
    version = project_service.save_working_copy_as_version(
        root.name, message="cache e2e")
    return version.version_id


def _run_and_read(
    project: Path, *, bust_cache: bool = False
) -> dict[str, pd.DataFrame]:
    manifest = execute_run(project / "runs", project.name, *pinned_stages(project), bust_cache=bust_cache)
    assert manifest["status"] == "ok", manifest
    run_dir = project / "runs" / manifest["run_id"]
    return {
        record["stage_id"]: pd.read_parquet(run_dir / record["output_path"])
        for record in manifest["stage_records"]
    }


def _run_and_count_replays(project: Path) -> dict[str, object]:
    manifest = execute_run(project / "runs", project.name, *pinned_stages(project))
    assert manifest["status"] == "ok", manifest
    return {
        record["stage_id"]: record.get("cached_rows")
        for record in manifest["stage_records"]
    }


def _invocations(probe: Path) -> Counter[str]:
    if not probe.exists():
        return Counter()
    return Counter(probe.read_text(encoding="utf-8").split())


def _assert_same_outputs(
    first: dict[str, pd.DataFrame], second: dict[str, pd.DataFrame]
) -> None:
    assert sorted(first) == sorted(second)
    for stage_id, frame in first.items():
        assert_frame_equal(second[stage_id], frame, obj=stage_id)


def test_a_second_run_replays_every_row_and_reproduces_the_first_exactly(tmp_path):
    probe = _write_project(tmp_path)
    _publish_a_version(tmp_path)

    first = _run_and_read(tmp_path)
    assert _invocations(probe) == _EVERYTHING_COMPUTED

    second = _run_and_read(tmp_path)
    # No row-mapped body ran again; the frame stage recomputed, as it always does.
    assert _invocations(probe) == _EVERYTHING_COMPUTED + _A_FURTHER_RUN
    _assert_same_outputs(first, second)


def test_the_manifest_counts_the_rows_the_second_run_replayed(tmp_path):
    """Only the row-mapped stages carry a count; a frame-shaped stage replays no rows."""
    _write_project(tmp_path)
    _publish_a_version(tmp_path)

    first = _run_and_count_replays(tmp_path)
    assert first == {"load": None, "clean": None, "flag": None, "totals": None}

    second = _run_and_count_replays(tmp_path)
    assert second == {
        "load": None, "clean": len(_ROWS), "flag": len(_ROWS), "totals": None}


def test_bust_cache_recomputes_everything_and_leaves_the_cache_re_pinned(tmp_path):
    """The busted run is given a row nothing has ever pinned, or the next run would replay anyway."""
    probe = _write_project(tmp_path)
    _publish_a_version(tmp_path)

    _run_and_read(tmp_path)
    assert _invocations(probe) == _EVERYTHING_COMPUTED

    _append_input_row(tmp_path, {"name": "d", "val": 4})
    busted = _run_and_read(tmp_path, bust_cache=True)
    # Every row recomputed, the three already pinned included: the read was skipped.
    assert _invocations(probe) == _EVERYTHING_COMPUTED + Counter(
        {"clean": 4, "flag": 4}) + _A_FURTHER_RUN

    after = _run_and_read(tmp_path)
    # No row body ran, so row "d" — computed by the busted run alone — was pinned by it.
    assert _invocations(probe) == _EVERYTHING_COMPUTED + Counter(
        {"clean": 4, "flag": 4}) + _A_FURTHER_RUN + _A_FURTHER_RUN
    _assert_same_outputs(busted, after)


def test_editing_one_stages_function_body_invalidates_that_stage_alone(tmp_path):
    """The edit changes `clean`'s fingerprint but not its output, so the stages below still replay."""
    probe = _write_project(tmp_path)
    _publish_a_version(tmp_path)
    first = _run_and_read(tmp_path)

    _write_project(tmp_path, clean_edit="\n# a comment the cache must notice\n")
    _publish_a_version(tmp_path)

    edited = _run_and_read(tmp_path)
    assert _invocations(probe) == _EVERYTHING_COMPUTED + Counter({"clean": 3}) + _A_FURTHER_RUN
    _assert_same_outputs(first, edited)


def test_one_new_input_row_recomputes_only_that_row(tmp_path):
    probe = _write_project(tmp_path)
    _publish_a_version(tmp_path)
    _run_and_read(tmp_path)
    _run_and_read(tmp_path)
    assert _invocations(probe) == _EVERYTHING_COMPUTED + _A_FURTHER_RUN

    _append_input_row(tmp_path, {"name": "d", "val": 4})
    _run_and_read(tmp_path)

    assert _invocations(probe) == _EVERYTHING_COMPUTED + Counter(
        {"clean": 1, "flag": 1}) + _A_FURTHER_RUN + _A_FURTHER_RUN


def test_a_stage_declaring_cache_false_recomputes_on_every_run(tmp_path):
    probe = _write_project(tmp_path, flag_cache=False)
    _publish_a_version(tmp_path)

    _run_and_read(tmp_path)
    _run_and_read(tmp_path)

    # `flag` re-rolled while `clean` above it replayed: the opt-out is that stage's alone.
    assert _invocations(probe) == _EVERYTHING_COMPUTED + Counter({"flag": 3}) + _A_FURTHER_RUN


def test_the_cache_survives_a_process_restart_and_a_change_of_directory(tmp_path):
    """Two different cwds: a frames root resolved against cwd would split the two runs' stores."""
    from app.core.persistence import configure_store
    from app.core.sqlite_store import SqliteKvStore

    db = tmp_path / "workspace" / "app.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    configure_store(SqliteKvStore(str(db)))  # the version must outlive this process too
    probe = _write_project(tmp_path)
    _publish_a_version(tmp_path)
    first_cwd, second_cwd = tmp_path / "launch_a", tmp_path / "launch_b"
    first_cwd.mkdir(parents=True, exist_ok=True)
    second_cwd.mkdir(parents=True, exist_ok=True)

    _run_in_a_fresh_process(tmp_path, db=db, cwd=first_cwd)
    assert _invocations(probe) == _EVERYTHING_COMPUTED

    _run_in_a_fresh_process(tmp_path, db=db, cwd=second_cwd)

    assert _invocations(probe) == _EVERYTHING_COMPUTED + _A_FURTHER_RUN  # every ROW replayed


def _run_in_a_fresh_process(project: Path, *, db: Path, cwd: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    env = {k: v for k, v in os.environ.items() if k != "CARBON_PAPER_FRAMES_ROOT"}
    result = subprocess.run(
        [sys.executable, "-m", "app.cli", project.name],
        cwd=cwd,
        env={**env, "PYTHONPATH": str(repo_root), "CARBON_PAPER_DB_PATH": str(db),
             # The CLI takes a project NAME, so the fresh process needs the root to
             # resolve it under — the one thing a different cwd must not change.
             "CARBON_PAPER_PROJECTS_DIR": str(project.parent)},
        capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        f"run crashed in a fresh process:\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


def test_a_first_run_of_a_fresh_project_replays_nothing(tmp_path):
    """Grounds the module: a count that does not grow is a replay, not a probe that never fired."""
    probe = _write_project(tmp_path)
    assert _invocations(probe) == _NOTHING_COMPUTED
    _publish_a_version(tmp_path)
    _run_and_read(tmp_path)
    assert _invocations(probe) == _EVERYTHING_COMPUTED

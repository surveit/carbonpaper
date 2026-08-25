"""One file per stage holds both halves, and says which of them the run recorded."""
from __future__ import annotations

import pytest

from app.core.frames import write_frame_table
from app.runtime.branches import RowBranches
from app.runtime.errors import LineageSidecarLengthMismatch
from app.runtime.lineage import RowLineage, RowParent
from app.runtime.lineage_sidecar import (
    read_lineage_sidecar,
    resolve_lineage_sidecar_path,
    write_lineage_sidecar,
)

_KEPT = RowLineage([[RowParent("filings", 2)], [RowParent("filings", 5)]])
_TAKEN = RowBranches([("should_include/0:if",), ("should_include/0:if",)])


def _outputs(tmp_path):
    (tmp_path / "outputs").mkdir(parents=True, exist_ok=True)
    return tmp_path


def test_both_halves_ride_in_one_file(tmp_path) -> None:
    run_dir = _outputs(tmp_path)
    write_lineage_sidecar(run_dir, "kept", _KEPT, _TAKEN)

    sidecar = read_lineage_sidecar(run_dir, "kept")
    assert sidecar.lineage is not None and sidecar.lineage.parents == _KEPT.parents
    assert sidecar.branches is not None and sidecar.branches.taken == _TAKEN.taken


def test_a_branches_only_stage_reports_no_lineage_rather_than_no_parents(tmp_path) -> None:
    run_dir = _outputs(tmp_path)
    # An empty parent list would read as summarized from nothing, stopping a trace.
    write_lineage_sidecar(run_dir, "tier", None, _TAKEN)

    sidecar = read_lineage_sidecar(run_dir, "tier")
    assert sidecar.lineage is None
    assert sidecar.branches is not None and sidecar.branches.taken == _TAKEN.taken


def test_a_lineage_only_stage_reports_no_branches(tmp_path) -> None:
    run_dir = _outputs(tmp_path)
    write_lineage_sidecar(run_dir, "kept", _KEPT, None)

    sidecar = read_lineage_sidecar(run_dir, "kept")
    assert sidecar.branches is None
    assert sidecar.lineage is not None and len(sidecar.lineage) == 2


def test_a_stage_that_recorded_neither_writes_nothing(tmp_path) -> None:
    run_dir = _outputs(tmp_path)
    write_lineage_sidecar(run_dir, "passthrough", None, None)

    assert not resolve_lineage_sidecar_path(run_dir, "passthrough").exists()
    assert read_lineage_sidecar(run_dir, "passthrough") == read_lineage_sidecar(run_dir, "absent")


def test_an_empty_stage_records_zero_rows_rather_than_nothing(tmp_path) -> None:
    run_dir = _outputs(tmp_path)
    write_lineage_sidecar(run_dir, "kept", RowLineage([]), RowBranches([]))

    sidecar = read_lineage_sidecar(run_dir, "kept")
    assert sidecar.lineage is not None and len(sidecar.lineage) == 0
    assert sidecar.branches is not None and len(sidecar.branches) == 0


def test_halves_of_different_lengths_are_refused(tmp_path) -> None:
    run_dir = _outputs(tmp_path)
    with pytest.raises(LineageSidecarLengthMismatch, match="2 row"):
        write_lineage_sidecar(run_dir, "kept", _KEPT, RowBranches([("should_include/0:if",)]))


def test_a_run_made_before_the_merge_still_shows_its_branches(tmp_path) -> None:
    run_dir = _outputs(tmp_path)
    write_frame_table(_KEPT.to_table(), run_dir / "outputs" / "kept.lineage.parquet")
    write_frame_table(_TAKEN.to_table(), run_dir / "outputs" / "kept.branch.parquet")

    sidecar = read_lineage_sidecar(run_dir, "kept")
    assert sidecar.lineage is not None and sidecar.lineage.parents == _KEPT.parents
    assert sidecar.branches is not None and sidecar.branches.taken == _TAKEN.taken


def test_a_branches_only_stage_of_such_a_run_still_reports_no_lineage(tmp_path) -> None:
    run_dir = _outputs(tmp_path)
    write_frame_table(_TAKEN.to_table(), run_dir / "outputs" / "tier.branch.parquet")

    sidecar = read_lineage_sidecar(run_dir, "tier")
    assert sidecar.lineage is None
    assert sidecar.branches is not None and sidecar.branches.taken == _TAKEN.taken

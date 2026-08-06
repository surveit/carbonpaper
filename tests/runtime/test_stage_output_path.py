"""The one join of a run dir to a recorded output_path: what it refuses. A manifest is
a file on disk, so `output_path` is untrusted input — a path leaving the run dir is not
read. tests/arch/test_stage_output_path_join_is_owned.py keeps every reader on this."""
from __future__ import annotations

import pandas as pd
import pytest
from fastapi import HTTPException

from app.core.errors import StageOutputMissing
from app.runtime.manifest import resolve_output_path
from app.web.loading import read_output_df


@pytest.fixture
def run_dir(tmp_path):
    directory = tmp_path / "demo" / "runs" / "20260101T000000"
    (directory / "outputs").mkdir(parents=True)
    return directory


def test_a_recorded_path_inside_the_run_dir_resolves(run_dir):
    assert resolve_output_path(run_dir, "outputs/load.parquet") == (
        run_dir / "outputs" / "load.parquet").resolve()


def test_a_record_naming_no_output_resolves_to_nothing(run_dir):
    assert resolve_output_path(run_dir, None) is None
    assert resolve_output_path(run_dir, "") is None


def test_a_recorded_path_climbing_out_of_the_run_dir_is_refused(run_dir):
    with pytest.raises(StageOutputMissing, match="escapes"):
        resolve_output_path(run_dir, "../../../../etc/passwd")


def test_a_symlink_out_of_the_run_dir_is_refused(run_dir):
    """Resolved, not string-matched: a link INSIDE the run dir can still point outside."""
    outside = run_dir.parent.parent / "secret.csv"
    outside.write_text("a\n1\n", encoding="utf-8")
    (run_dir / "outputs" / "load.csv").symlink_to(outside)
    with pytest.raises(StageOutputMissing, match="escapes"):
        resolve_output_path(run_dir, "outputs/load.csv")


def test_a_sibling_run_whose_name_starts_with_this_one_is_refused(run_dir):
    """Containment, not a string prefix: `.../20260101T0000009` is a DIFFERENT run."""
    sibling = run_dir.parent / f"{run_dir.name}9"
    sibling.mkdir()
    (sibling / "load.csv").write_text("a\n1\n", encoding="utf-8")
    with pytest.raises(StageOutputMissing, match="escapes"):
        resolve_output_path(run_dir, f"../{sibling.name}/load.csv")


def test_the_web_reader_turns_a_refused_path_into_a_404(run_dir):
    """A corrupt manifest is a bad request for that stage's rows, not a 500."""
    with pytest.raises(HTTPException) as caught:
        read_output_df(run_dir, "../../../../etc/passwd")
    assert caught.value.status_code == 404


def test_a_csv_output_is_read_as_csv(run_dir):
    """The executor writes CSV where parquet cannot hold a frame, so the suffix decides."""
    pd.DataFrame({"a": [1, 2]}).to_csv(run_dir / "outputs" / "load.csv", index=False)
    assert len(read_output_df(run_dir, "outputs/load.csv")) == 2

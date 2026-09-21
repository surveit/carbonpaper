"""What a capture writes, and the states it refuses rather than capturing a lie."""
from __future__ import annotations

import pytest

from app.core.run_status import RunStatus
from app.models.records.project import Project
from app.services import methodology
from app.services.run import read_run_manifest
from app.services.run_restore import CAPTURED_ARCHIVE, CAPTURED_INPUTS
from claim_review_fixture import PROJECT, run_the_fixture
from scripts.errors import RunCaptureRefused
from scripts.run_capture import capture_run

_SOURCE_STAGES = {"load_east": "east.csv", "load_west": "west.csv",
                  "load_agencies": "agencies.csv"}


@pytest.fixture
def run_id(projects_root) -> str:
    finished = run_the_fixture(projects_root)
    # The scope fixture writes no project record; the archive export needs one.
    Project(id=PROJECT, name=PROJECT, model="sonnet", source="test").save()
    methodology.write_methodology(PROJECT, "Twelve grants, read as one set.")
    return finished


def test_a_capture_writes_the_archive_beside_the_files_each_input_stage_read(
    projects_root, run_id, tmp_path
):
    into = tmp_path / "capture"

    capture_run(PROJECT, run_id, into)

    assert (into / CAPTURED_ARCHIVE).read_bytes()[:2] == b"PK"
    copied = {path.parent.name: path.name
              for path in (into / CAPTURED_INPUTS).glob("*/*")}
    assert copied == _SOURCE_STAGES


def test_a_capture_copies_each_file_byte_for_byte(projects_root, run_id, tmp_path):
    into = tmp_path / "capture"

    capture_run(PROJECT, run_id, into)

    for stage_id, filename in _SOURCE_STAGES.items():
        assert (into / CAPTURED_INPUTS / stage_id / filename).read_bytes() == (
            projects_root / PROJECT / "data" / filename).read_bytes()


def test_a_run_that_did_not_finish_clean_is_refused(run_id, tmp_path):
    manifest = read_run_manifest(PROJECT, run_id)
    manifest.status = RunStatus.ERRORS
    manifest.save()

    with pytest.raises(RunCaptureRefused, match="errors"):
        capture_run(PROJECT, run_id, tmp_path / "capture")


def test_a_capture_names_the_stage_whose_file_went_missing(
    projects_root, run_id, tmp_path
):
    (projects_root / PROJECT / "data" / "west.csv").unlink()

    with pytest.raises(RunCaptureRefused, match="load_west"):
        capture_run(PROJECT, run_id, tmp_path / "capture")

"""What a capture writes, and the states it refuses rather than capturing a lie."""
from __future__ import annotations

import pytest

from app.core.run_status import RunStatus
from app.models.captured_run import CapturedRun
from app.models.records.project import Project
from app.services import methodology
from app.services.errors import RunCaptureRefused
from app.services.run import read_run_manifest
from app.services.run_capture import (
    CAPTURED_ARCHIVE,
    CAPTURED_INPUTS,
    CAPTURED_RECORD,
    capture_run,
)
from claim_review_fixture import PROJECT, run_the_fixture

_SOURCE_STAGES = {"load_east": "east.csv", "load_west": "west.csv",
                  "load_agencies": "agencies.csv"}


@pytest.fixture
def run_id(projects_root) -> str:
    finished = run_the_fixture(projects_root)
    # The scope fixture writes no project record; the archive export needs one.
    Project(id=PROJECT, name=PROJECT, model="sonnet", source="test").save()
    methodology.write_methodology(PROJECT, "Twelve grants, read as one set.")
    return finished


def test_a_clean_capture_round_trips_its_record(run_id, tmp_path):
    into = tmp_path / "capture"

    captured = capture_run(PROJECT, run_id, into)

    assert CapturedRun.model_validate_json(
        (into / CAPTURED_RECORD).read_text(encoding="utf-8")) == captured
    assert captured.project_name == PROJECT and captured.run_id == run_id
    assert (into / CAPTURED_ARCHIVE).read_bytes()[:2] == b"PK"


def test_a_capture_carries_every_file_each_input_stage_read(
    projects_root, run_id, tmp_path
):
    into = tmp_path / "capture"

    captured = capture_run(PROJECT, run_id, into)

    assert {i.stage_id: i.file.filename for i in captured.inputs} == _SOURCE_STAGES
    for stage_id, filename in _SOURCE_STAGES.items():
        copied = into / CAPTURED_INPUTS / stage_id / filename
        source = projects_root / PROJECT / "data" / filename
        assert copied.read_bytes() == source.read_bytes()


def test_the_record_holds_what_the_run_measured(projects_root, run_id, tmp_path):
    captured = capture_run(PROJECT, run_id, tmp_path / "capture")

    for entry in captured.inputs:
        source = projects_root / PROJECT / "data" / entry.file.filename
        assert entry.file.bytes == source.stat().st_size
        assert entry.file.path == str(source)


def test_a_run_that_did_not_finish_clean_is_refused(run_id, tmp_path):
    manifest = read_run_manifest(PROJECT, run_id)
    manifest.status = RunStatus.ERRORS
    manifest.save()

    with pytest.raises(RunCaptureRefused, match="errors"):
        capture_run(PROJECT, run_id, tmp_path / "capture")


def test_a_source_file_edited_since_the_run_is_refused(projects_root, run_id, tmp_path):
    edited = projects_root / PROJECT / "data" / "east.csv"
    edited.write_text(edited.read_text(encoding="utf-8") + "G-099,east,AGENCY-A,1,grant\n",
                      encoding="utf-8")

    with pytest.raises(RunCaptureRefused, match="changed since the run"):
        capture_run(PROJECT, run_id, tmp_path / "capture")


def test_a_capture_names_the_stage_whose_file_went_missing(
    projects_root, run_id, tmp_path
):
    (projects_root / PROJECT / "data" / "west.csv").unlink()

    with pytest.raises(RunCaptureRefused, match="load_west"):
        capture_run(PROJECT, run_id, tmp_path / "capture")

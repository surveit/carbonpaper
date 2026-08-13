"""The project↔file edge: a file has at most one project, and the record holding the
bytes carries nothing about which. Content addressing is unaffected — two projects
sending identical bytes still share one copy on disk, through a file record each."""
from __future__ import annotations

import hashlib
import io

import pytest

from app.core.files import ProjectFile, StoredFile
from app.services.errors import FileHeldByAnotherProject
from app.services.uploads import (
    claim_file_for_project,
    delete_file,
    find_holding_project,
    list_project_files,
    resolve_stored_path,
    save_upload,
)

_CSV = b"name,val\nx,1\n"
_CSV_SHA = hashlib.sha256(_CSV).hexdigest()


def _upload(project_id: str | None = None, name: str = "posts.csv",
            body: bytes = _CSV) -> StoredFile:
    return save_upload(name, io.BytesIO(body), project_id)


# ── the file record says nothing about a project ─────────────────────────────

def test_a_stored_file_carries_no_project_field():
    assert "project_id" not in StoredFile.model_fields


def test_an_upload_records_a_file_and_the_edge_that_holds_it():
    record = _upload("demo")

    assert (record.sha256, record.filename) == (_CSV_SHA, "posts.csv")
    (edge,) = ProjectFile.list()
    assert (edge.project_id, edge.file_id) == ("demo", record.id)


def test_an_upload_into_no_project_records_no_edge():
    assert find_holding_project(_upload().id) is None
    assert ProjectFile.list() == []


# ── the uniqueness check ─────────────────────────────────────────────────────

def test_a_file_cannot_be_claimed_by_a_second_project():
    record = _upload("demo")

    with pytest.raises(FileHeldByAnotherProject, match="held by project 'demo'"):
        claim_file_for_project(record.id, "other")


def test_the_project_already_holding_a_file_may_claim_it_again():
    record = _upload("demo")
    first = claim_file_for_project(record.id, "demo")

    again = claim_file_for_project(record.id, "demo")

    # Re-saving would restamp created_at, which is when this project got the file.
    assert (again.id, again.created_at) == (first.id, first.created_at)
    assert len(ProjectFile.list()) == 1


def test_the_refused_claim_leaves_the_first_project_holding_it():
    record = _upload("demo")
    with pytest.raises(FileHeldByAnotherProject):
        claim_file_for_project(record.id, "other")

    assert find_holding_project(record.id) == "demo"
    assert [r.sha256 for r in list_project_files("other")] == []


# ── which is not a rule about bytes ──────────────────────────────────────────

def test_two_projects_sending_the_same_bytes_get_a_file_each_over_one_copy():
    demo, other = _upload("demo"), _upload("other")

    assert demo.id != other.id
    assert resolve_stored_path(demo) == resolve_stored_path(other)
    assert {find_holding_project(demo.id), find_holding_project(other.id)} == {"demo", "other"}


def test_one_project_dropping_shared_bytes_leaves_the_other_readable():
    demo, other = _upload("demo"), _upload("other")

    delete_file("demo", _CSV_SHA)

    assert list_project_files("demo") == []
    assert resolve_stored_path(other).read_bytes() == _CSV
    assert StoredFile.load_or_none(demo.id) is None
    assert [edge.project_id for edge in ProjectFile.list()] == ["other"]


def test_the_last_project_dropping_the_bytes_takes_them_off_disk():
    record = _upload("demo")
    path = resolve_stored_path(record)

    delete_file("demo", _CSV_SHA)

    assert not path.exists()
    assert ProjectFile.list() == []

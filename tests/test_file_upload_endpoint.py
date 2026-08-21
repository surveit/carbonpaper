"""POST /project/{name}/files — one multipart endpoint behind the run form's Browse…
button and reachable by any HTTP caller. The browser hands over bytes and never a
path, so the server saves them in the workspace's one store and answers with the
record: the file id naming it, and the path a run reads it from."""
from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import workspace
from app.core.errors import FileNotStoredError
from app.core.files import (
    UploadedFile,
    move_file_to_project,
    files_root,
    list_project_files,
    max_upload_bytes,
    resolve_stored_path,
    save_upload,
)
from app.services.uploads import resolve_files_binding

client = TestClient(app)

CSV = b"name,val\nx,1\n"
CSV_SHA = hashlib.sha256(CSV).hexdigest()


@pytest.fixture
def project(tmp_path, monkeypatch):
    proj = tmp_path / "demo"
    proj.mkdir(parents=True)
    (tmp_path / "other").mkdir(parents=True)
    workspace.set_projects_dir(tmp_path)
    # The store sits beside the document store, not under a project.
    monkeypatch.setenv("CARBON_PAPER_FILES_ROOT", str(tmp_path / "files"))
    return proj


def _only_record() -> UploadedFile:
    records = UploadedFile.list()
    assert len(records) == 1, f"expected one record, got {len(records)}"
    return records[0]


def upload(name: str, body: bytes, project_name: str = "demo"):
    return client.post(
        f"/project/{project_name}/files",
        files={"file": (name, body, "text/csv")},
    )


def test_upload_saves_under_the_records_id_and_returns_the_path(project):
    body = upload("posts.csv", CSV).json()
    assert body["ok"] is True
    saved = Path(body["path"])
    assert saved == (files_root() / body["file_id"] / "posts.csv").resolve()
    assert saved.read_bytes() == CSV  # bytes landed intact


def test_the_response_names_the_file_for_a_caller_that_is_not_the_browser(project):
    body = upload("posts.csv", CSV).json()
    # An agent that can run curl gets what it needs to name the file later — no HTML,
    # no path parsing.
    assert body["file_id"] == _only_record().id
    assert body["filename"] == "posts.csv"
    assert body["bytes"] == len(CSV)
    assert body["uploaded_at"]
    assert body["label"].startswith(
        "Uploaded "
    )
    assert body["label"].endswith(f" · posts.csv · {len(CSV)}B")


def test_the_returned_path_is_read_back_off_the_record_alone(project):
    upload("posts.csv", CSV)
    record = _only_record()
    assert resolve_stored_path(record) == (files_root() / record.id / "posts.csv").resolve()


def test_the_stored_filename_keeps_the_extension_a_binding_reads_the_format_from(project):
    saved = Path(upload("2026-lobbying.xlsx", b"PK\x03\x04").json()["path"])
    # _collect_bindings resolves the run's file format off this suffix, so a
    # hash-named copy with no extension would fail the trigger.
    assert saved.suffix == ".xlsx"
    assert saved.name == "2026-lobbying.xlsx"


def test_same_bytes_twice_is_two_records_over_two_copies(project):
    """Sending the same file again is a second arrival, free to have come from elsewhere."""
    first = upload("posts.csv", CSV).json()
    second = upload("posts.csv", CSV).json()
    assert first["file_id"] != second["file_id"]
    assert first["path"] != second["path"]
    for body in (first, second):
        assert Path(body["path"]).read_bytes() == CSV


def test_different_bytes_do_not_overwrite_each_other(project):
    one = Path(upload("a.csv", b"one").json()["path"])
    two = Path(upload("a.csv", b"two").json()["path"])
    assert one != two
    assert one.read_bytes() == b"one" and two.read_bytes() == b"two"


def test_no_temp_file_is_left_behind(project):
    upload("posts.csv", CSV)
    leftovers = [p.name for p in (files_root()).iterdir() if p.name.startswith(".")]
    assert leftovers == []


def test_the_record_names_the_file_the_human_picked(project):
    upload("posts.csv", CSV)
    record = _only_record()
    # The hash is still recorded — as evidence about the bytes, not as their address.
    assert record.sha256 == CSV_SHA
    assert record.filename == "posts.csv"
    assert record.byte_count == len(CSV)


def test_re_picking_the_same_bytes_is_a_second_arrival_with_its_own_time(project):
    upload("posts.csv", CSV)
    first = _only_record()
    upload("renamed.csv", CSV)
    again = next(r for r in UploadedFile.list() if r.id != first.id)
    # Two arrivals of one file are two events. Collapsing them lost which upload a run
    # read and where each came from, which is the whole reason the record is the address.
    assert again.created_at > first.created_at
    assert (first.filename, again.filename) == ("posts.csv", "renamed.csv")
    assert again.sha256 == first.sha256


def test_filename_is_basename_sanitized(project):
    # A crafted name must not escape the directory it is written to.
    saved = Path(upload("../../etc/evil.csv", b"x").json()["path"])
    assert saved.parent.parent == files_root().resolve()
    assert saved.name == "evil.csv"


def test_a_nameless_upload_still_stores(project):
    # Path("..").name is "..", which would climb out of the record's directory.
    saved = Path(upload("..", b"x").json()["path"])
    assert saved.name == "upload.dat"
    assert saved.parent.parent == files_root().resolve()


def test_a_file_over_the_ceiling_is_refused_and_leaves_nothing_behind(project, monkeypatch):
    monkeypatch.setenv("CARBON_PAPER_MAX_UPLOAD_BYTES", "64")
    resp = upload("big.csv", b"x" * 65)
    assert resp.status_code == 400
    assert "over the 64B limit for a single input" in resp.json()["error"]
    assert list(files_root().iterdir()) == []  # no partial, no temp file


def test_a_file_exactly_at_the_ceiling_is_kept(project, monkeypatch):
    monkeypatch.setenv("CARBON_PAPER_MAX_UPLOAD_BYTES", "64")
    assert upload("fits.csv", b"x" * 64).status_code == 200


def test_the_quota_refuses_the_upload_that_would_cross_it(project, monkeypatch):
    monkeypatch.setenv("CARBON_PAPER_FILES_QUOTA_BYTES", "100")
    assert upload("a.csv", b"a" * 80).status_code == 200
    resp = upload("b.csv", b"b" * 80)
    assert resp.status_code == 400
    assert "over the 100B limit" in resp.json()["error"]
    stored = [p.name for p in files_root().rglob("*") if p.is_file()]
    assert stored == ["a.csv"]  # the refused one is not kept, and neither is a temp file


def test_the_quota_spans_projects_because_one_store_does(project, monkeypatch):
    monkeypatch.setenv("CARBON_PAPER_FILES_QUOTA_BYTES", "100")
    assert upload("a.csv", b"a" * 80, project_name="demo").status_code == 200
    # A second project shares the disk, so it shares the limit — a per-project quota
    # over one store would let N projects each spend the whole thing.
    assert upload("b.csv", b"b" * 80, project_name="other").status_code == 400


def test_re_sending_bytes_the_store_already_holds_spends_the_quota_again(project, monkeypatch):
    monkeypatch.setenv("CARBON_PAPER_FILES_QUOTA_BYTES", "100")
    assert upload("a.csv", b"a" * 80).status_code == 200
    # A second copy lands on disk, so the same bytes again cost the same 80 bytes. The
    # store buys provenance with disk, and the quota reports what is actually spent.
    assert upload("a.csv", b"a" * 80).status_code == 400


def test_a_limit_that_is_not_a_positive_number_fails_loudly(project, monkeypatch):
    monkeypatch.setenv("CARBON_PAPER_MAX_UPLOAD_BYTES", "0")
    with pytest.raises(ValueError, match="must be a positive whole number"):
        max_upload_bytes()


def test_missing_file_is_422(project):
    # FastAPI rejects a missing required File before the handler runs.
    assert client.post("/project/demo/files").status_code == 422


def test_unknown_project_404(project):
    assert upload("a.csv", b"x", project_name="nope").status_code == 404


# ─── The project link, and the file that has none yet ────────────────────────

def test_an_upload_through_a_project_route_is_claimed_by_that_project(project):
    upload("posts.csv", CSV)
    assert [r.filename for r in list_project_files("demo")] == ["posts.csv"]
    assert list_project_files(None) == []


def test_two_projects_sending_the_same_bytes_each_get_their_own_copy(project):
    demo = Path(upload("posts.csv", CSV, project_name="demo").json()["path"])
    other = Path(upload("posts.csv", CSV, project_name="other").json()["path"])
    assert demo != other  # a copy each: deleting one cannot empty the other
    assert {r.project_id for r in UploadedFile.list()} == {"demo", "other"}


def test_a_file_can_arrive_before_any_project_owns_it(project):
    record = save_upload("posts.csv", io.BytesIO(CSV))
    assert record.project_id is None
    assert [r.sha256 for r in list_project_files(None)] == [CSV_SHA]
    assert list_project_files("demo") == []


def test_claiming_moves_no_bytes(project):
    record = save_upload("posts.csv", io.BytesIO(CSV))
    before = resolve_stored_path(record)
    claimed = move_file_to_project(record.id, "demo")
    assert claimed.project_id == "demo"
    assert resolve_stored_path(claimed) == before  # the path never depended on the project
    assert list_project_files(None) == []
    assert [r.filename for r in list_project_files("demo")] == ["posts.csv"]


def test_claiming_a_file_no_project_is_missing_fails_loudly(project):
    file_id = upload("posts.csv", CSV).json()["file_id"]  # already claimed by demo
    with pytest.raises(FileNotStoredError, match="outside a project"):
        move_file_to_project(file_id, "other")


def test_a_run_cannot_bind_a_file_another_project_holds(project):
    file_id = upload("posts.csv", CSV, project_name="demo").json()["file_id"]
    assert resolve_files_binding("demo", [file_id])["paths"][0].endswith("posts.csv")
    with pytest.raises(FileNotStoredError, match="has no file"):
        resolve_files_binding("other", [file_id])


def test_a_binding_carries_the_format_the_extension_names(project):
    file_id = upload("posts.csv", CSV).json()["file_id"]
    assert resolve_files_binding("demo", [file_id])["format"] == "csv"


def test_a_tsv_upload_binds_as_tsv(project):
    body = b"name\tval\nx\t1\n"
    file_id = upload("posts.tsv", body).json()["file_id"]
    assert resolve_files_binding("demo", [file_id])["format"] == "tsv"


def test_one_files_bytes_sent_again_under_another_name_binds_as_that_other_name(project):
    body = b"name,val\nx,1\n"
    tsv = upload("posts.tsv", body).json()
    csv = upload("posts.csv", body).json()
    # Two records over identical bytes, each read as the format its own name says.
    assert resolve_files_binding("demo", [tsv["file_id"]]) == {"paths": [tsv["path"]],
                                                               "format": "tsv"}
    assert resolve_files_binding("demo", [csv["file_id"]]) == {"paths": [csv["path"]],
                                                               "format": "csv"}


def test_files_of_two_formats_are_refused_rather_than_half_read(project):
    """They become ONE table, so one reader — a mixed set has no single answer."""
    csv = upload("posts.csv", b"a,b\n1,2\n").json()
    tsv = upload("other.tsv", b"a\tb\n1\t2\n").json()
    with pytest.raises(ValueError, match="share a format"):
        resolve_files_binding("demo", [csv["file_id"], tsv["file_id"]])

"""POST /project/{name}/files — one multipart endpoint behind the run form's Browse…
button and reachable by any HTTP caller. The browser hands over bytes and never a
path, so the server saves them in the workspace's one content-addressed store and
answers with the record: the sha256 naming the file, and the path a run reads it from."""
from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import workspace
from app.services.errors import FileNotStoredError
from app.services.uploads import (
    UploadedFile,
    move_file_to_project,
    files_root,
    list_project_files,
    max_upload_bytes,
    resolve_file_binding,
    resolve_stored_path,
    save_upload,
)

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


def test_upload_saves_under_the_content_hash_and_returns_the_path(project):
    body = upload("posts.csv", CSV).json()
    assert body["ok"] is True
    saved = Path(body["path"])
    assert saved == (files_root() / CSV_SHA / "posts.csv").resolve()
    assert saved.read_bytes() == CSV  # bytes landed intact


def test_the_response_names_the_file_for_a_caller_that_is_not_the_browser(project):
    body = upload("posts.csv", CSV).json()
    # An agent that can run curl gets what it needs to name the file later, and the
    # hash to check the bytes it just sent against — no HTML, no path parsing.
    assert body["sha256"] == CSV_SHA
    assert body["filename"] == "posts.csv"
    assert body["bytes"] == len(CSV)


def test_the_returned_path_is_read_back_off_the_record_alone(project):
    upload("posts.csv", CSV)
    record = _only_record()
    assert resolve_stored_path(record) == (files_root() / CSV_SHA / "posts.csv").resolve()


def test_the_stored_filename_keeps_the_extension_a_binding_reads_the_format_from(project):
    saved = Path(upload("2026-lobbying.xlsx", b"PK\x03\x04").json()["path"])
    # _collect_bindings resolves the run's file format off this suffix, so a
    # hash-named copy with no extension would fail the trigger.
    assert saved.suffix == ".xlsx"
    assert saved.name == "2026-lobbying.xlsx"


def test_same_bytes_twice_is_one_copy(project):
    first = upload("posts.csv", CSV).json()["path"]
    second = upload("posts.csv", CSV).json()["path"]
    assert first == second
    assert [p.name for p in (files_root() / CSV_SHA).iterdir()] == ["posts.csv"]


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
    assert record.sha256 == CSV_SHA
    assert record.filename == "posts.csv"
    assert record.byte_count == len(CSV)


def test_re_picking_the_same_bytes_keeps_the_first_arrival_time(project):
    upload("posts.csv", CSV)
    first = _only_record()
    upload("renamed.csv", CSV)
    again = _only_record()
    assert again.created_at == first.created_at  # same bytes, first seen once
    assert again.filename == "renamed.csv"       # the latest pick names it
    assert again.updated_at > first.updated_at


def test_filename_is_basename_sanitized(project):
    # A crafted name must not escape the hash dir it is written to.
    saved = Path(upload("../../etc/evil.csv", b"x").json()["path"])
    assert saved.parent.parent == files_root().resolve()
    assert saved.name == "evil.csv"


def test_a_nameless_upload_still_stores(project):
    # Path("..").name is "..", which would climb out of the hash dir.
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


def test_re_sending_bytes_the_store_already_holds_is_allowed_at_quota(project, monkeypatch):
    monkeypatch.setenv("CARBON_PAPER_FILES_QUOTA_BYTES", "100")
    first = upload("a.csv", b"a" * 80).json()["path"]
    # Same bytes: content addressing means this costs no disk, so the quota must not
    # refuse it — the reader would be told to delete a file to re-pick what is there.
    again = upload("a.csv", b"a" * 80)
    assert again.status_code == 200 and again.json()["path"] == first


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


def test_two_projects_sending_the_same_bytes_share_one_copy_and_hold_two_claims(project):
    demo = Path(upload("posts.csv", CSV, project_name="demo").json()["path"])
    other = Path(upload("posts.csv", CSV, project_name="other").json()["path"])
    assert demo == other  # one blob on disk
    assert {r.project_id for r in UploadedFile.list()} == {"demo", "other"}  # two claims


def test_a_file_can_arrive_before_any_project_owns_it(project):
    record = save_upload("posts.csv", io.BytesIO(CSV))
    assert record.project_id is None
    assert [r.sha256 for r in list_project_files(None)] == [CSV_SHA]
    assert list_project_files("demo") == []


def test_claiming_moves_no_bytes(project):
    record = save_upload("posts.csv", io.BytesIO(CSV))
    before = resolve_stored_path(record)
    claimed = move_file_to_project(CSV_SHA, "demo")
    assert claimed.project_id == "demo"
    assert resolve_stored_path(claimed) == before  # the path never depended on the project
    assert list_project_files(None) == []
    assert [r.filename for r in list_project_files("demo")] == ["posts.csv"]


def test_claiming_a_file_no_project_is_missing_fails_loudly(project):
    upload("posts.csv", CSV)  # already claimed by demo
    with pytest.raises(FileNotStoredError, match="outside a project"):
        move_file_to_project(CSV_SHA, "other")


def test_a_run_cannot_bind_a_file_another_project_holds(project):
    upload("posts.csv", CSV, project_name="demo")
    assert resolve_file_binding("demo", CSV_SHA)["path"].endswith("posts.csv")
    with pytest.raises(FileNotStoredError, match="has no file"):
        resolve_file_binding("other", CSV_SHA)


def test_a_binding_carries_the_format_the_extension_names(project):
    upload("posts.csv", CSV)
    assert resolve_file_binding("demo", CSV_SHA)["format"] == "csv"

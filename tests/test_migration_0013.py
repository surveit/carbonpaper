"""0013 moves stored bytes from <root>/<sha256>/ to <root>/<record id>/."""
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from scripts.uploaded_file_addresses import (
    FileBytesMissing,
    StoredFile,
    move_store_to_record_addresses,
)

_REVISION = (Path(__file__).resolve().parents[1]
             / "alembic/versions/0013_address_files_by_record_id.py")

_CSV = b"name,val\nx,1\n"
_SHA = hashlib.sha256(_CSV).hexdigest()


def _load_revision() -> Any:
    spec = importlib.util.spec_from_file_location("_rev_0013", _REVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_blob(root: Path, sha256: str, held_name: str, body: bytes) -> Path:
    """The store as content addressing wrote it: one directory per hash, one file inside."""
    blob = root / sha256
    blob.mkdir(parents=True, exist_ok=True)
    (blob / held_name).write_bytes(body)
    return blob


def test_each_of_several_records_over_one_blob_ends_up_with_its_own_bytes(tmp_path):
    """The whole reason this copies rather than moves; a real store may have no such blob."""
    _write_blob(tmp_path, _SHA, "posts.csv", _CSV)
    records = [StoredFile(id=f"rec{n}", sha256=_SHA, filename=f"posts-{n}.csv")
               for n in range(3)]

    assert sorted(move_store_to_record_addresses(tmp_path, records)) == [
        "rec0", "rec1", "rec2"]

    for n in range(3):
        own = tmp_path / f"rec{n}" / f"posts-{n}.csv"
        assert own.read_bytes() == _CSV
    # Three copies now, where content addressing kept one. That is the trade, not a bug.
    assert not (tmp_path / _SHA).exists()


def test_the_bytes_are_read_off_the_name_on_disk_not_the_records_filename(tmp_path):
    _write_blob(tmp_path, _SHA, "posts.csv", _CSV)
    # The old store wrote a blob under the FIRST name it arrived as and never rewrote it,
    # so a record renamed since names a file that was never on disk.
    record = StoredFile(id="rec", sha256=_SHA, filename="posts-renamed.csv")

    move_store_to_record_addresses(tmp_path, [record])

    assert (tmp_path / "rec" / "posts-renamed.csv").read_bytes() == _CSV
    assert not (tmp_path / "rec" / "posts.csv").exists()


def test_a_record_whose_bytes_are_gone_names_itself_and_both_paths(tmp_path):
    record = StoredFile(id="rec", sha256=_SHA, filename="posts.csv")

    with pytest.raises(FileBytesMissing, match=r"rec \('posts.csv'\) has no bytes"):
        move_store_to_record_addresses(tmp_path, [record])

    # Refused, not papered over: no empty file left standing in for the missing bytes.
    assert not (tmp_path / "rec").exists()


def test_one_record_missing_its_bytes_stops_the_whole_run(tmp_path):
    _write_blob(tmp_path, _SHA, "posts.csv", _CSV)
    present = StoredFile(id="here", sha256=_SHA, filename="posts.csv")
    absent = StoredFile(id="gone", sha256="0" * 64, filename="other.csv")

    with pytest.raises(FileBytesMissing, match="gone"):
        move_store_to_record_addresses(tmp_path, [present, absent])

    # The blob it did copy is still where it was: nothing is removed until every copy is in.
    assert (tmp_path / _SHA / "posts.csv").read_bytes() == _CSV


def test_running_it_again_moves_nothing_and_raises_nothing(tmp_path):
    _write_blob(tmp_path, _SHA, "posts.csv", _CSV)
    record = StoredFile(id="rec", sha256=_SHA, filename="posts.csv")
    move_store_to_record_addresses(tmp_path, [record])

    assert move_store_to_record_addresses(tmp_path, [record]) == []

    assert (tmp_path / "rec" / "posts.csv").read_bytes() == _CSV


def test_a_blob_directory_no_record_names_is_left_alone(tmp_path):
    """A store can hold blobs no record covers; those bytes are not this migration's to drop."""
    _write_blob(tmp_path, _SHA, "posts.csv", _CSV)
    orphan = _write_blob(tmp_path, "f" * 64, "stray.csv", b"stray")

    move_store_to_record_addresses(tmp_path, [StoredFile(
        id="rec", sha256=_SHA, filename="posts.csv")])

    assert (orphan / "stray.csv").read_bytes() == b"stray"


def test_a_blob_directory_holding_a_file_no_record_names_survives(tmp_path):
    blob = _write_blob(tmp_path, _SHA, "posts.csv", _CSV)
    # Two names in one directory is what a store written before the de-duplication fix
    # looks like; only the file a record was copied from goes.
    (blob / "zz-other.csv").write_bytes(b"other")

    move_store_to_record_addresses(tmp_path, [StoredFile(
        id="rec", sha256=_SHA, filename="posts.csv")])

    assert (blob / "zz-other.csv").read_bytes() == b"other"
    assert not (blob / "posts.csv").exists()


@pytest.mark.parametrize("field", ["id", "sha256", "filename"])
@pytest.mark.parametrize("value", ["..", "a/b", "", "."])
def test_a_field_that_is_not_a_plain_name_is_refused_not_sanitised(field, value):
    payload = {"id": "rec", "sha256": _SHA, "filename": "posts.csv", field: value}

    with pytest.raises(ValidationError, match="not a plain name"):
        StoredFile.model_validate(payload)


def test_the_revision_refuses_to_go_back():
    rev = _load_revision()

    with pytest.raises(NotImplementedError, match="not reversible"):
        rev.downgrade()

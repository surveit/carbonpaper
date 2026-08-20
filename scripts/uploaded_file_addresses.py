"""Re-addressing the file store: the bytes an uploaded_file record holds move out of a
directory named by their sha256 and into one named by the record's own id.

Shared by alembic revision 0013 and its test, which is the only thing that ever
exercises several records over one blob — a real store may or may not have any.
"""
from __future__ import annotations

import shutil
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel, field_validator


class FileBytesMissing(FileNotFoundError):
    """A record with bytes at neither its old content address nor its new one."""


class StoredFile(BaseModel):
    """The three fields of an uploaded_file row that re-addressing its bytes needs."""

    id: str
    sha256: str
    filename: str

    @field_validator("filename", "sha256", "id")
    @classmethod
    def _refuse_anything_but_a_plain_name(cls, value: str) -> str:
        # Each of the three becomes a path component below, so a separator or a `..` in
        # any of them would write outside the store.
        if value in ("", ".", "..") or value != Path(value).name:
            raise ValueError(f"not a plain name, so unsafe as a path component: {value!r}")
        return value


def move_store_to_record_addresses(root: Path, records: Sequence[StoredFile]) -> list[str]:
    """Re-address every record's bytes under `root`; returns the ids whose bytes moved."""
    # Every copy runs before any removal: content addressing let several records share
    # one blob, and moving it would leave all but the first record with no bytes at all.
    sources = {record.id: _copy_to_record_address(root, record) for record in records}
    for source in {source for source in sources.values() if source is not None}:
        source.unlink()
        _remove_if_empty(source.parent)
    return [file_id for file_id, source in sources.items() if source is not None]


def _copy_to_record_address(root: Path, record: StoredFile) -> Path | None:
    """The blob file copied from, or None when this record already sits at its own address."""
    destination = root / record.id / record.filename
    source = _find_stored_file(root / record.sha256)
    if source is None:
        if destination.is_file():
            return None
        raise FileBytesMissing(
            f"uploaded_file {record.id} ('{record.filename}') has no bytes at "
            f"{root / record.sha256} and none at {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    return source


def _find_stored_file(blob_dir: Path) -> Path | None:
    """The name the bytes were WRITTEN under, which a record's `filename` may differ from."""
    if not blob_dir.is_dir():
        return None
    return next((entry for entry in sorted(blob_dir.iterdir()) if entry.is_file()), None)


def _remove_if_empty(directory: Path) -> None:
    # A blob directory holding anything no record named keeps it: those bytes are the
    # only copy, and this migration is not the thing that decides they are rubbish.
    if directory.is_dir() and not any(directory.iterdir()):
        directory.rmdir()

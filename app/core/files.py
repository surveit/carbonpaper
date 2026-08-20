"""Data files a run reads: one content-addressed store for the whole workspace, the
record that says which project holds each one, and the two size limits that keep a
run loadable and the volume from filling."""
from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import BinaryIO, ClassVar

from app.core.errors import FileNotStoredError, FileOverCeiling, StoreOverQuota
from app.core.persistence import PersistedModel, PersistenceScope
from app.core.store_config import resolve_db_path

# How much of an upload is held in memory at once while it is written and hashed.
_CHUNK_BYTES = 1024 * 1024

# The name a file with no usable one of its own is stored under.
_FALLBACK_FILENAME = "upload.dat"

_KILOBYTE = 1024
_MEGABYTE = 1024 * _KILOBYTE
_GIGABYTE = 1024 * _MEGABYTE

# What one input file may weigh. The upload itself is not what this protects — it
# streams in fixed-size chunks and would take any size. The run is: input_data
# hands a csv/json/xlsx source to pandas whole, and a 500MB csv measured a 566MB
# frame with the read peaking near 1GB, on a machine with 2GiB for the whole app.
# A larger file is one this app can accept and then never load, which is worse
# than refusing it. Raise it with the machine, not on its own.
_DEFAULT_MAX_UPLOAD_BYTES = 512 * _MEGABYTE

# What every stored file may weigh together. One store serves the workspace, so this
# bounds the disk the document store, the frames and every run's outputs share — which
# a per-project number could not, since several projects at their own limit exceed it.
_DEFAULT_FILES_QUOTA_BYTES = 4 * _GIGABYTE


# `sha256` addresses the BYTES and `project_id` says which project holds them, so the two
# are separate: one blob serves however many projects uploaded it, each with its own
# record and its own filename for it. `project_id` is None for a file that arrived before
# any project existed — `move_file_to_project` fills it in, moving nothing on disk.
class UploadedFile(PersistedModel):
    """One project's hold on one stored file; `id` is incidental, (sha256, project) is the key."""

    collection: ClassVar[str] = "uploaded_file"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.PROJECT_READ

    sha256: str
    filename: str
    byte_count: int
    project_id: str | None = None


def files_root() -> Path:
    """Beside the document store and the frames, so pinning the DB path carries it too."""
    override = os.environ.get("CARBON_PAPER_FILES_ROOT")
    return Path(override) if override is not None else resolve_db_path().parent / "files"


def max_upload_bytes() -> int:
    return _read_byte_limit("CARBON_PAPER_MAX_UPLOAD_BYTES", _DEFAULT_MAX_UPLOAD_BYTES)


def files_quota_bytes() -> int:
    return _read_byte_limit("CARBON_PAPER_FILES_QUOTA_BYTES", _DEFAULT_FILES_QUOTA_BYTES)


def save_upload(filename: str, src: BinaryIO, project_id: str | None = None) -> UploadedFile:
    """Store an uploaded file and return its record; `project_id` None puts it in no project."""
    root = files_root()
    # Content-addressed, so the destination is not known until the last byte is read:
    # the stream is written to a temp file in the same dir and moved into
    # <root>/<sha256>/<filename> once its hash is. The sha256 owns the directory
    # rather than the file name so the name a human chose survives into every path we
    # show them — the run form's field, and the packet's "inputs this run read".
    root.mkdir(parents=True, exist_ok=True)
    safe_name = _safe_filename(filename)
    staged, digest, byte_count = _write_to_temp_file(root, src, max_upload_bytes())
    # One copy per sha256, WHATEVER it was called this time: the same bytes sent again
    # under a new name used to land beside the first as a second file, so the directory
    # was content-addressed but its contents were not. `held` is that first name, and it
    # is what the record keeps pointing at — resolve_stored_path builds from it.
    if _find_stored_name(root / digest) is not None:
        staged.unlink()
        return _record_upload(digest, safe_name, byte_count, project_id)
    # Checked here rather than before the read, so re-sending bytes the store already
    # holds costs nothing and is allowed at quota. The overshoot while deciding is one
    # file's worth, bounded by the ceiling above.
    _refuse_upload_over_quota(root, staged, digest, byte_count)
    (root / digest).mkdir(parents=True, exist_ok=True)
    staged.replace(root / digest / safe_name)
    return _record_upload(digest, safe_name, byte_count, project_id)


def _find_stored_name(blob_dir: Path) -> str | None:
    """None means these bytes are new to the store, so the caller has to write them."""
    if not blob_dir.is_dir():
        return None
    return next((f.name for f in sorted(blob_dir.iterdir()) if f.is_file()), None)


def move_file_to_project(sha256: str, project_id: str) -> UploadedFile:
    """Move a file with no project into one. Moves no bytes — the store is shared."""
    without_project = _find_records(sha256=sha256, project_id=None)
    if not without_project:
        raise FileNotStoredError(
            f"no file {sha256!r} outside a project — it is either already in one, or was "
            "never uploaded")
    record = without_project[0]
    record.project_id = project_id
    record.save()
    return record


def delete_file(project_id: str | None, sha256: str) -> None:
    """Drop one project's hold on a file, and the bytes when nothing else holds them."""
    records = _find_records(sha256=sha256, project_id=project_id)
    if not records:
        raise FileNotStoredError(
            f"no file {sha256!r} in {project_id or 'the files outside a project'} — "
            "nothing to delete")
    path = resolve_stored_path(records[0])
    for record in records:
        UploadedFile.delete(record.id)
    # Content addressing means one blob can serve several projects, so the bytes go only
    # when the last hold on them does — otherwise deleting here empties another project.
    if not _find_any_record(sha256) and path.is_file():
        path.unlink()
        _remove_if_empty(path.parent)


def resolve_stored_path(record: UploadedFile) -> Path:
    """The file these bytes were FIRST stored as, which `record.filename` may have moved on from."""
    blob_dir = files_root() / record.sha256
    # `filename` is the name of the latest send, shown to whoever picked it. The bytes
    # were written once, under whatever they were called then, and that name is never
    # rewritten — a run manifest holds the path it read, so renaming would strand it.
    held = _find_stored_name(blob_dir)
    return (blob_dir / (held or record.filename)).resolve()


def list_project_files(project_id: str | None) -> list[UploadedFile]:
    """One project's files, or those in no project when `project_id` is None."""
    return _sorted_newest_first(_find_records(project_id=project_id))


def open_project_file(project_id: str, sha256: str) -> tuple[UploadedFile, Path]:
    """The record and the readable path, or loud — never a path whose bytes are gone."""
    records = _find_records(sha256=sha256, project_id=project_id)
    if not records:
        raise FileNotStoredError(
            f"project '{project_id}' has no file {sha256!r} — list its files, upload this "
            "one, or move it in if it is not in a project yet")
    path = resolve_stored_path(records[0])
    # A record whose bytes are gone is worse than no record: the caller would act on a
    # path naming a file it was just told the project had.
    if not path.is_file():
        raise FileNotStoredError(
            f"'{records[0].filename}' is recorded for project '{project_id}' but its bytes "
            f"are not on disk at {path} — upload it again")
    return records[0], path


def measure_files_used_bytes() -> int:
    """What the store weighs, read off the records rather than by walking the disk."""
    return _sum_stored_bytes({})


def _sum_stored_bytes(arriving: dict[str, int]) -> int:
    # Content-addressed: two projects holding one blob are two records over ONE copy.
    weighed = {record.sha256: record.byte_count for record in UploadedFile.list()}
    # `arriving` is bytes already on disk that no record covers yet — a file mid-upload.
    return sum((weighed | arriving).values())


def _find_records(*, sha256: str | None = None,
                  project_id: str | None = None) -> list[UploadedFile]:
    records = UploadedFile.list()
    # A full scan: the store selects by id prefix only, and the key here is
    # (sha256, project_id). Fine at a workspace's worth of files, not at thousands.
    return [record for record in records
            if (sha256 is None or record.sha256 == sha256)
            and record.project_id == project_id]


def _find_any_record(sha256: str) -> list[UploadedFile]:
    return [record for record in UploadedFile.list() if record.sha256 == sha256]


def _remove_if_empty(directory: Path) -> None:
    if directory.is_dir() and not any(directory.iterdir()):
        directory.rmdir()


def _sorted_newest_first(records: list[UploadedFile]) -> list[UploadedFile]:
    return sorted(records, key=lambda record: record.created_at, reverse=True)


def _write_to_temp_file(root: Path, src: BinaryIO, ceiling: int) -> tuple[Path, str, int]:
    """Stream `src` to a temp file beside its destination; returns (path, sha256, bytes)."""
    digest = hashlib.sha256()
    byte_count = 0
    # mkstemp in `root` itself, so the move into place is a rename within one
    # filesystem and a reader never sees a partly written file at the real path.
    handle, temp_name = tempfile.mkstemp(dir=root, prefix=".incoming-")
    temp = Path(temp_name)
    with os.fdopen(handle, "wb") as out:
        while chunk := src.read(_CHUNK_BYTES):
            byte_count += len(chunk)
            if byte_count > ceiling:
                # Unlinking an open file is safe here: the fd stays valid until the
                # `with` closes it, and nothing is left behind to sweep up later.
                temp.unlink()
                raise FileOverCeiling(ceiling=ceiling)
            digest.update(chunk)
            out.write(chunk)
    return temp, digest.hexdigest(), byte_count


def _refuse_upload_over_quota(
    root: Path, staged: Path, digest: str, byte_count: int
) -> None:
    quota = files_quota_bytes()
    # The arriving bytes are staged on disk and have no record yet, so they are passed
    # in rather than read back — this is the same figure list_files reports as remaining.
    used = _sum_stored_bytes({digest: byte_count})
    if used > quota:
        staged.unlink()
        raise StoreOverQuota(used=used, quota=quota, sent=byte_count, root=root)


def _record_upload(digest: str, filename: str, byte_count: int,
                   project_id: str | None) -> UploadedFile:
    stored = _find_records(sha256=digest, project_id=project_id)
    # Re-saving a stored record rather than replacing it keeps `created_at` at the first
    # arrival of these bytes for this project.
    if not stored:
        record = UploadedFile(sha256=digest, filename=filename,
                              byte_count=byte_count, project_id=project_id)
    else:
        record = stored[0]
        record.filename = filename
    record.save()
    return record


def _safe_filename(raw: str) -> str:
    """The basename alone, so an uploaded name cannot escape the dir it is written to."""
    name = Path(raw).name
    # Path('../..').name is '..', not '' — a basename is not on its own a safe component.
    return _FALLBACK_FILENAME if name in ("", ".", "..") else name


def _read_byte_limit(variable: str, fallback: int) -> int:
    configured = os.environ.get(variable)
    if configured is None:
        return fallback
    # A limit that is not a positive whole number is a deployment mistake, and
    # falling back to the default would hide it behind a limit nobody chose.
    if not configured.isdecimal() or int(configured) <= 0:
        raise ValueError(f"{variable} must be a positive whole number of bytes, "
                         f"got {configured!r}")
    return int(configured)

"""Data files a run reads: one store for the whole workspace, addressed by the record
that holds each file, and the two size limits that keep a run loadable and the volume
from filling."""
from __future__ import annotations

import enum
import hashlib
import os
import tempfile
from pathlib import Path
from typing import BinaryIO, ClassVar

from app.core.errors import FileNotStoredError, FileOverCeiling, StoreOverQuota
from app.core.record import PersistedModel, PersistenceScope
from app.core.store_config import resolve_db_path
from app.core.ids import ID

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


# A claim about the DATA, not the work; see docs/run-and-review-ui.md.
class FileCompleteness(enum.StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    SAMPLED = "sampled"


# The record's own `id` addresses the bytes: they sit in a directory of that name, alone.
# `sha256` is EVIDENCE about them, never their address — two sends of identical bytes are
# two records over two copies, each free to say where and when it came from. That costs
# the disk one copy per send and is what buys the provenance. `project_id` is None for a
# file that arrived before any project existed — `move_file_to_project` fills it in,
# moving nothing on disk.
class ProjectFile(PersistedModel):
    """One project's file. `id` names the bytes' directory; `sha256` is what they hashed to."""

    collection: ClassVar[str] = "uploaded_file"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.PROJECT_READ

    sha256: str
    filename: str
    byte_count: int
    project_id: ID | None = None
    completeness: FileCompleteness = FileCompleteness.OPEN
    lineage: str = ""


def files_root() -> Path:
    """Beside the document store and the frames, so pinning the DB path carries it too."""
    override = os.environ.get("CARBON_PAPER_FILES_ROOT")
    return Path(override) if override is not None else resolve_db_path().parent / "files"


def max_upload_bytes() -> int:
    return _read_byte_limit("CARBON_PAPER_MAX_UPLOAD_BYTES", _DEFAULT_MAX_UPLOAD_BYTES)


def files_quota_bytes() -> int:
    return _read_byte_limit("CARBON_PAPER_FILES_QUOTA_BYTES", _DEFAULT_FILES_QUOTA_BYTES)


def save_upload(filename: str, src: BinaryIO, project_id: ID | None = None) -> ProjectFile:
    """Store an uploaded file and return its record; `project_id` None puts it in no project."""
    root = files_root()
    # The stream is written to a temp file in the same dir first and moved into
    # <root>/<record id>/<filename> once there is a record to name the directory. The
    # record owns the directory rather than the file name, so the name a human chose
    # survives into every path we show them — the run form's field, and the packet's
    # "inputs this run read".
    root.mkdir(parents=True, exist_ok=True)
    staged, digest, byte_count = _write_to_temp_file(root, src, max_upload_bytes())
    _refuse_upload_over_quota(root, staged, byte_count)
    record = ProjectFile(sha256=digest, filename=_safe_filename(filename),
                        byte_count=byte_count, project_id=project_id)
    (root / record.id).mkdir(parents=True, exist_ok=True)
    staged.replace(resolve_stored_path(record))
    # Saved only once the bytes are in place: a record whose bytes are missing is what
    # open_project_file exists to refuse, while bytes no record covers cost only disk.
    record.save()
    return record


def move_file_to_project(file_id: ID, project_id: ID) -> ProjectFile:
    """Move a file with no project into one. Moves no bytes — the record is the address."""
    record = ProjectFile.load_or_none(file_id)
    if record is None or record.project_id is not None:
        raise FileNotStoredError(
            f"no file {file_id!r} outside a project — it is either already in one, or was "
            "never uploaded")
    record.project_id = project_id
    record.save()
    return record


def update_file_provenance(
    project_id: ID, file_id: ID, completeness: FileCompleteness, lineage: str,
) -> ProjectFile:
    """`sampled` takes a lineage note saying how the sample was drawn; the others do not."""
    if completeness == FileCompleteness.SAMPLED and not lineage.strip():
        raise ValueError(
            "a sampled file needs a note saying how the sample was drawn — say which rows "
            "these are, or set completeness to open")
    record = ProjectFile.load_or_none(file_id)
    if record is None or record.project_id != project_id:
        raise FileNotStoredError(
            f"no file {file_id!r} in project '{project_id}' — list its files, upload this "
            "one, or move it in if it is not in a project yet")
    record.completeness = completeness
    record.lineage = lineage
    record.save()
    return record


def delete_file(project_id: ID | None, file_id: ID) -> None:
    """Delete one file: its record, and the bytes that record alone owns."""
    record = ProjectFile.load_or_none(file_id)
    if record is None or record.project_id != project_id:
        raise FileNotStoredError(
            f"no file {file_id!r} in {project_id or 'the files outside a project'} — "
            "nothing to delete")
    path = resolve_stored_path(record)
    ProjectFile.delete(record.id)
    if path.is_file():
        path.unlink()
    _delete_if_empty(path.parent)


def resolve_stored_path(record: ProjectFile) -> Path:
    """The record owns its directory, so its `filename` is the only file inside it."""
    return (files_root() / record.id / record.filename).resolve()


def find_stored_file(project_id: ID, path: str) -> ProjectFile | None:
    """None for a path the store never held: a run may read anywhere on disk."""
    stored = Path(path)
    if stored.parent.parent != files_root():
        return None
    record = ProjectFile.load_or_none(stored.parent.name)
    if record is None or record.project_id != project_id:
        return None
    return record if record.filename == stored.name else None


def list_project_files(project_id: ID | None) -> list[ProjectFile]:
    """One project's files, or those in no project when `project_id` is None."""
    return _sorted_newest_first(ProjectFile.find(project_id=project_id))


def open_project_file(project_id: ID, file_id: ID) -> tuple[ProjectFile, Path]:
    """The record and the readable path, or loud — never a path whose bytes are gone."""
    record = ProjectFile.load_or_none(file_id)
    if record is None or record.project_id != project_id:
        raise FileNotStoredError(
            f"project '{project_id}' has no file {file_id!r} — list its files, upload this "
            "one, or move it in if it is not in a project yet")
    path = resolve_stored_path(record)
    # A record whose bytes are gone is worse than no record: the caller would act on a
    # path naming a file it was just told the project had.
    if not path.is_file():
        raise FileNotStoredError(
            f"'{record.filename}' is recorded for project '{project_id}' but its bytes "
            f"are not on disk at {path} — upload it again")
    return record, path


def measure_files_used_bytes() -> int:
    """What the store weighs, read off the records rather than by walking the disk."""
    return sum(record.byte_count for record in ProjectFile.list())


def _delete_if_empty(directory: Path) -> None:
    if directory.is_dir() and not any(directory.iterdir()):
        directory.rmdir()


def _sorted_newest_first(records: list[ProjectFile]) -> list[ProjectFile]:
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


def _refuse_upload_over_quota(root: Path, staged: Path, byte_count: int) -> None:
    quota = files_quota_bytes()
    # The arriving bytes are staged on disk and have no record yet, so they are added in
    # rather than read back — this is the same figure list_files reports as remaining.
    used = measure_files_used_bytes() + byte_count
    if used > quota:
        staged.unlink()
        raise StoreOverQuota(used=used, quota=quota, sent=byte_count, root=root)


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

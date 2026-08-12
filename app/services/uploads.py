"""Data files a run reads: one content-addressed store for the whole workspace, the
record that says which project claims each one, and the two size limits that keep a
run loadable and the volume from filling."""
from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import BinaryIO, ClassVar

from app.core.persistence import PersistedModel, PersistenceScope
from app.core.store_config import resolve_db_path
from app.models.schema import TypeUnsafeUserStageConfigOverride
from app.models.stages.input_data import resolve_file_format
from app.services.errors import FileNotStoredError, UploadTooLargeError

# How much of an upload is held in memory at once while it is written and hashed.
_CHUNK_BYTES = 1024 * 1024

# The name a file with no usable one of its own is stored under.
_FALLBACK_FILENAME = "upload.dat"

_MEGABYTE = 1024 * 1024
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


# `sha256` addresses the BYTES and `project_id` says who claims them, so the two are
# separate: one blob serves however many projects uploaded it, each with its own record
# and its own filename for it. `project_id` is None for a file that arrived before any
# project owned it — `claim_file` fills it in, moving nothing on disk.
class UploadedFile(PersistedModel):
    """One project's claim on one stored file; `id` is incidental, (sha256, project) is the key."""

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
    """Store an uploaded file and return the record of it; `project_id` None leaves it unclaimed."""
    root = files_root()
    # Content-addressed, so the destination is not known until the last byte is read:
    # the stream is written to a temp file in the same dir and moved into
    # <root>/<sha256>/<filename> once its hash is. The sha256 owns the directory
    # rather than the file name so the name a human chose survives into every path we
    # show them — the run form's field, and the packet's "inputs this run read".
    root.mkdir(parents=True, exist_ok=True)
    safe_name = _safe_filename(filename)
    staged, digest, byte_count = _write_to_temp_file(root, src, max_upload_bytes())
    dest = root / digest / safe_name
    # The quota is checked here rather than before the read, so re-sending bytes the
    # store already holds costs nothing and is allowed at quota. The overshoot while
    # deciding is one file's worth, bounded by the ceiling above.
    if dest.exists():
        staged.unlink()
    else:
        _refuse_upload_over_quota(root, staged, byte_count)
        dest.parent.mkdir(parents=True, exist_ok=True)
        staged.replace(dest)
    return _record_upload(digest, safe_name, byte_count, project_id)


def claim_file(sha256: str, project_id: str) -> UploadedFile:
    """Give an unclaimed file to a project. Moves no bytes — the store is shared."""
    unclaimed = _find_records(sha256=sha256, project_id=None)
    if not unclaimed:
        raise FileNotStoredError(
            f"no unclaimed file {sha256!r} — it is either already in a project, or was "
            "never uploaded")
    record = unclaimed[0]
    record.project_id = project_id
    record.save()
    return record


def resolve_stored_path(record: UploadedFile) -> Path:
    """Where a stored file sits, read back off its record alone."""
    return (files_root() / record.sha256 / record.filename).resolve()


def list_project_files(project_id: str) -> list[UploadedFile]:
    """One project's files, newest arrival first."""
    return _sorted_newest_first(_find_records(project_id=project_id))


def list_unclaimed_files() -> list[UploadedFile]:
    """Files no project has taken yet, newest arrival first."""
    return _sorted_newest_first(_find_records(project_id=None))


def resolve_file_binding(project_id: str, sha256: str) -> TypeUnsafeUserStageConfigOverride:
    """The connector params a run of `project_id` binds for one of its files."""
    records = _find_records(sha256=sha256, project_id=project_id)
    if not records:
        raise FileNotStoredError(
            f"project '{project_id}' has no file {sha256!r} — list its files, upload this "
            "one, or claim it if it is not in a project yet")
    path = resolve_stored_path(records[0])
    # A record whose bytes are gone is worse than no record: the run would bind a path
    # and fail at preflight, naming a file the caller was just told it had.
    if not path.is_file():
        raise FileNotStoredError(
            f"'{records[0].filename}' is recorded for project '{project_id}' but its bytes "
            f"are not on disk at {path} — upload it again")
    return {"path": str(path), "format": resolve_file_format(str(path)).value}


def measure_files_used_bytes() -> int:
    """What the store weighs, counted off the disk it occupies rather than off the records."""
    root = files_root()
    if not root.is_dir():
        return 0
    # Off the disk, not by summing byte_count: two projects claiming one blob are two
    # records over one copy, so the records would double-count the bytes this bounds.
    return sum(f.stat().st_size for f in root.rglob("*") if f.is_file())


def _find_records(*, sha256: str | None = None,
                  project_id: str | None = None) -> list[UploadedFile]:
    records = UploadedFile.list()
    # A full scan: the store selects by id prefix only, and the key here is
    # (sha256, project_id). Fine at a workspace's worth of files, not at thousands.
    return [record for record in records
            if (sha256 is None or record.sha256 == sha256)
            and record.project_id == project_id]


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
                raise UploadTooLargeError(
                    f"this file is over the {describe_bytes(ceiling)} limit for a single "
                    "input. That ceiling is what a run on this machine can load into "
                    "memory, so a larger file would upload and then fail every run that "
                    "read it. Cut the file down, or convert it to parquet."
                )
            digest.update(chunk)
            out.write(chunk)
    return temp, digest.hexdigest(), byte_count


def _refuse_upload_over_quota(root: Path, staged: Path, byte_count: int) -> None:
    quota = files_quota_bytes()
    used = sum(f.stat().st_size for f in root.rglob("*") if f.is_file())
    # `used` counts the staged copy too — it is on the disk this bounds.
    if used > quota:
        staged.unlink()
        raise UploadTooLargeError(
            f"stored files would reach {describe_bytes(used)}, over the "
            f"{describe_bytes(quota)} limit — the {describe_bytes(byte_count)} just sent "
            f"was not kept. Every file before it was, and nothing in the app deletes one: "
            f"clear {root} on the server, or raise CARBON_PAPER_FILES_QUOTA_BYTES."
        )


def _record_upload(digest: str, filename: str, byte_count: int,
                   project_id: str | None) -> UploadedFile:
    stored = _find_records(sha256=digest, project_id=project_id)
    # Re-saving a stored record rather than replacing it keeps `created_at` at the first
    # arrival of these bytes for this project; only the name of the latest send differs.
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


def describe_bytes(count: int) -> str:
    """A size for a person reading a refusal, so 512MB is not shown as 536870912."""
    if count >= _GIGABYTE:
        return f"{count / _GIGABYTE:.3g}GB"
    if count >= _MEGABYTE:
        return f"{count / _MEGABYTE:.3g}MB"
    return f"{count}B"


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

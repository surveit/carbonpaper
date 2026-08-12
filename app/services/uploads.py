"""Run-input files uploaded through the browser: a content-addressed store under
a project's `files/` dir, the record of what that store holds, and the two
size limits that keep a run loadable and the volume from filling."""
from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import BinaryIO, ClassVar

from app.core.persistence import PersistedModel, PersistenceScope
from app.models.schema import TypeUnsafeUserStageConfigOverride
from app.models.stages.input_data import resolve_file_format
from app.services.errors import FileNotStoredError, UploadTooLargeError
from app.services.workspace import resolve_project_dir

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

# What one project's uploads may weigh in total. Bounds the volume the projects
# tree, the frame store and every run's outputs also live on.
_DEFAULT_PROJECT_QUOTA_BYTES = 2 * _GIGABYTE


# A record exists only once the bytes are fully written and hashed, so it is the
# one signal that a copy under files/ is complete rather than half streamed.
# `created_at` is when these bytes first arrived and `updated_at` when they were
# last picked; `filename` is the name of the most recent pick, which is what the
# run form and the review packet show the reader.
class UploadedFile(PersistedModel):
    """One stored upload, keyed `f"{project}/{sha256}"`."""

    collection: ClassVar[str] = "uploaded_file"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.PROJECT_READ

    sha256: str
    filename: str
    byte_count: int


def max_upload_bytes() -> int:
    return _read_byte_limit("CARBON_PAPER_MAX_UPLOAD_BYTES", _DEFAULT_MAX_UPLOAD_BYTES)


def project_quota_bytes() -> int:
    return _read_byte_limit("CARBON_PAPER_PROJECT_UPLOAD_QUOTA_BYTES",
                            _DEFAULT_PROJECT_QUOTA_BYTES)


def save_upload(project: str, filename: str, src: BinaryIO) -> UploadedFile:
    """Store an uploaded run-input file and return the record of it."""
    files_dir = resolve_project_dir(project) / "files"
    # Content-addressed, so the destination is not known until the last byte is
    # read: the stream is written to a temp file in the same dir and moved into
    # files/<sha256>/<filename> once its hash is. Picking the same file twice
    # lands on one copy and one record. The sha256 owns the directory rather than
    # the file name so the name a human chose survives into every path we show
    # them — the run form's field, and the review packet's "inputs this run read".
    files_dir.mkdir(parents=True, exist_ok=True)
    safe_name = _safe_filename(filename)
    staged, digest, byte_count = _write_to_temp_file(files_dir, src, max_upload_bytes())
    dest = files_dir / digest / safe_name
    # The quota is checked here rather than before the read, so that re-picking a
    # file the project already holds costs nothing and is allowed at quota. The
    # overshoot while deciding is one file's worth, bounded by the ceiling above.
    if dest.exists():
        staged.unlink()
    else:
        _refuse_upload_over_quota(files_dir, staged, byte_count)
        dest.parent.mkdir(parents=True, exist_ok=True)
        staged.replace(dest)
    return _record_upload(project, digest, safe_name, byte_count)


def resolve_stored_path(record: UploadedFile) -> Path:
    """Where a stored file sits, read back off its record alone."""
    project = record.id.split("/", 1)[0]
    return (resolve_project_dir(project) / "files" / record.sha256 / record.filename).resolve()


def list_project_files(project: str) -> list[UploadedFile]:
    """Newest arrival first."""
    return sorted(UploadedFile.list(f"{project}/"),
                  key=lambda record: record.created_at, reverse=True)


def resolve_file_binding(project: str, sha256: str) -> TypeUnsafeUserStageConfigOverride:
    """The connector params a run binds for one stored file."""
    record = UploadedFile.load_or_none(f"{project}/{sha256}")
    if record is None:
        raise FileNotStoredError(
            f"project '{project}' holds no file {sha256!r} — list its files, or upload "
            "this one first")
    path = resolve_stored_path(record)
    # A record whose bytes are gone is worse than no record: the run would bind a
    # path and fail at preflight, naming a file the caller was just told it had.
    if not path.is_file():
        raise FileNotStoredError(
            f"'{record.filename}' is recorded for project '{project}' but its bytes are "
            f"not on disk at {path} — upload it again")
    return {"path": str(path), "format": resolve_file_format(str(path)).value}


def measure_files_used_bytes(project: str) -> int:
    """What this project's stored files weigh, counted off the disk they occupy."""
    files_dir = resolve_project_dir(project) / "files"
    if not files_dir.is_dir():
        return 0
    return sum(f.stat().st_size for f in files_dir.rglob("*") if f.is_file())


def _write_to_temp_file(files_dir: Path, src: BinaryIO, ceiling: int) -> tuple[Path, str, int]:
    """Stream `src` to a temp file beside its destination; returns (path, sha256, bytes)."""
    digest = hashlib.sha256()
    byte_count = 0
    # mkstemp in `files_dir` itself, so the move into place is a rename within one
    # filesystem and a reader never sees a partly written file at the real path.
    handle, temp_name = tempfile.mkstemp(dir=files_dir, prefix=".incoming-")
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


def _refuse_upload_over_quota(files_dir: Path, staged: Path, byte_count: int) -> None:
    quota = project_quota_bytes()
    used = sum(f.stat().st_size for f in files_dir.rglob("*") if f.is_file())
    # `used` counts the staged copy too — it is on the disk this bounds.
    if used > quota:
        staged.unlink()
        raise UploadTooLargeError(
            f"this project's stored files would reach {describe_bytes(used)}, over its "
            f"{describe_bytes(quota)} limit — the {describe_bytes(byte_count)} just sent "
            "was not kept. Every upload before it was, and nothing in the app deletes "
            f"one: clear {files_dir} on the server, or raise "
            "CARBON_PAPER_PROJECT_UPLOAD_QUOTA_BYTES."
        )


def _record_upload(project: str, digest: str, filename: str, byte_count: int) -> UploadedFile:
    stored = UploadedFile.load_or_none(f"{project}/{digest}")
    # Re-saving a stored record rather than replacing it keeps `created_at` at the
    # first arrival of these bytes; only the name of the latest pick can differ.
    if stored is None:
        stored = UploadedFile(id=f"{project}/{digest}", sha256=digest,
                              filename=filename, byte_count=byte_count)
    else:
        stored.filename = filename
    stored.save()
    return stored


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

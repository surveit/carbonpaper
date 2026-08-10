"""Run-input files uploaded through the browser: a content-addressed store under
a project's `uploads/` dir, and the record of what that store holds."""
from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import BinaryIO, ClassVar

from app.core.persistence import PersistedModel, PersistenceScope
from app.services.workspace import resolve_project_dir

# How much of an upload is held in memory at once while it is written and hashed.
_CHUNK_BYTES = 1024 * 1024

# The name a file with no usable one of its own is stored under.
_FALLBACK_FILENAME = "upload.dat"


# A record exists only once the bytes are fully written and hashed, so it is the
# one signal that a copy under uploads/ is complete rather than half streamed.
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


def save_upload(project: str, filename: str, src: BinaryIO) -> Path:
    """Store an uploaded run-input file; returns the absolute path a run binds to."""
    uploads = resolve_project_dir(project) / "uploads"
    # Content-addressed, so the destination is not known until the last byte is
    # read: the stream is written to a temp file in the same dir and moved into
    # uploads/<sha256>/<filename> once its hash is. Picking the same file twice
    # lands on one copy and one record. The sha256 owns the directory rather than
    # the file name so the name a human chose survives into every path we show
    # them — the run form's field, and the review packet's "inputs this run read".
    uploads.mkdir(parents=True, exist_ok=True)
    safe_name = _safe_filename(filename)
    staged, digest, byte_count = _write_to_temp_file(uploads, src)
    dest = uploads / digest / safe_name
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        staged.unlink()
    else:
        staged.replace(dest)
    _record_upload(project, digest, safe_name, byte_count)
    return dest.resolve()


def _write_to_temp_file(uploads: Path, src: BinaryIO) -> tuple[Path, str, int]:
    """Stream `src` to a temp file beside its destination; returns (path, sha256, bytes)."""
    digest = hashlib.sha256()
    byte_count = 0
    # mkstemp in `uploads` itself, so the move into place is a rename within one
    # filesystem and a reader never sees a partly written file at the real path.
    handle, temp_name = tempfile.mkstemp(dir=uploads, prefix=".incoming-")
    with os.fdopen(handle, "wb") as out:
        while chunk := src.read(_CHUNK_BYTES):
            digest.update(chunk)
            byte_count += len(chunk)
            out.write(chunk)
    return Path(temp_name), digest.hexdigest(), byte_count


def _record_upload(project: str, digest: str, filename: str, byte_count: int) -> None:
    stored = UploadedFile.load_or_none(f"{project}/{digest}")
    # Re-saving a stored record rather than replacing it keeps `created_at` at the
    # first arrival of these bytes; only the name of the latest pick can differ.
    if stored is None:
        UploadedFile(id=f"{project}/{digest}", sha256=digest,
                     filename=filename, byte_count=byte_count).save()
        return
    stored.filename = filename
    stored.save()


def _safe_filename(raw: str) -> str:
    """The basename alone, so an uploaded name cannot escape the dir it is written to."""
    name = Path(raw).name
    # Path('../..').name is '..', not '' — a basename is not on its own a safe component.
    return _FALLBACK_FILENAME if name in ("", ".", "..") else name

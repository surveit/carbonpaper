"""The 0013 migration's two conversions, apart from the SQL that drives them.

A `TableRef` used to hold a path; it now holds the sha256 of a stored file. The bytes
those paths name still exist on the machine being migrated, so the conversion is a read
and a content-addressed write — nothing is invented, and a path that resolves nowhere
stops the migration rather than becoming an eval with no dataset.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from app.core.paths import CARBON_PAPER_HOME, repo_root
from app.services.uploads import files_root

_FALLBACK_FILENAME = "upload.dat"


class EvalDatasetMissing(FileNotFoundError):
    """A stored `TableRef.path` naming bytes neither root holds, so no sha256 can be honest."""


def find_table_refs(document: object) -> list[dict[str, object]]:
    """Every `TableRef` an eval config carries: its own table, then each reference override."""
    if not isinstance(document, dict):
        return []
    refs = [document.get("table")]
    overrides = document.get("reference_overrides")
    if isinstance(overrides, list):
        refs += [override.get("table") for override in overrides
                 if isinstance(override, dict)]
    return [ref for ref in refs if isinstance(ref, dict) and "path" in ref]


# Two roots, because the field outlived the one it was written against: `read_table_ref`
# resolved it under the checkout, and the store moved out of the checkout to
# ~/.carbonpaper, taking `examples/` — and every project-relative path recorded here —
# with it. The seeded `app/seeds/…` datasets are the ones still under the checkout.
def locate_dataset_bytes(path: str) -> Path:
    candidates = [repo_root() / path, CARBON_PAPER_HOME / path]
    found = next((candidate for candidate in candidates if candidate.is_file()), None)
    if found is None:
        raise EvalDatasetMissing(
            f"eval dataset {path!r} is on neither {candidates[0]} nor {candidates[1]}. "
            "A TableRef now names stored bytes, and there are none to name: put the file "
            "at one of those paths, or delete the eval's `table` before migrating")
    return found


class EvalResultRefUnreadable(ValueError):
    """A stored `result_ref` in neither shape this migration knows how to move."""


# `result_ref` used to be relative to the PROJECT and is now relative to the run that
# wrote it, so the whole conversion is dropping the `eval_run/<run id>/` the runner put
# in front of it. Read structurally rather than off the disk: a run whose result file
# has since been cleaned up still has a ref, and it must move with the rest.
def strip_run_prefix(result_ref: str, run_id: str) -> str:
    prefix = f"eval_run/{run_id}/"
    if not result_ref.startswith(prefix):
        raise EvalResultRefUnreadable(
            f"eval run {run_id!r} records result_ref {result_ref!r}, which does not start "
            f"with {prefix!r} — nothing wrote that shape, so what it is relative to is "
            "unknown and no rewrite of it would be honest")
    return result_ref[len(prefix):]


def store_dataset_bytes(source: Path) -> tuple[str, str, int]:
    """Copy a dataset into the file store the way an upload lands; returns (sha256, name, bytes)."""
    content = source.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    filename = source.name if source.name not in ("", ".", "..") else _FALLBACK_FILENAME
    blob_dir = files_root() / digest
    blob_dir.mkdir(parents=True, exist_ok=True)
    # One copy per sha256, under whatever it was first called — the same rule
    # `save_upload` writes by, so a later upload of these bytes finds them already here.
    if not any(child.is_file() for child in blob_dir.iterdir()):
        (blob_dir / filename).write_bytes(content)
    return digest, filename, len(content)

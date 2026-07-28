"""The DataFrame layer: parquet storage for the tabular payloads that aren't
documents — run stage outputs, review-queue snapshots, decision logs, and
uploaded eval datasets — plus the frame-to-rows conversion every caller that
walks a frame row by row shares. Storage uses the same (collection, id)
addressing as the document store, different physical form: one parquet file per
frame under a root directory. The only place outside the document store that
turns an id into a file path, so it reuses validate_id."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from app.core.persistence import validate_id

# The on-disk extension for a frame file, named so every reader that
# distinguishes a parquet output from a csv one (by `Path.suffix`) compares
# against the same value instead of re-typing the literal.
PARQUET_SUFFIX = ".parquet"


def list_rows(frame: pd.DataFrame) -> list[dict[str, Any]]:
    """`frame` as one dict per row, column label → cell value. The labels are
    pinned to `str`: pandas types them as `Hashable`, so a caller that keys a row
    by a column name would otherwise be working against a wider type than any
    frame read from parquet or CSV actually carries."""
    return [
        {str(label): value for label, value in record.items()}
        for record in frame.to_dict("records")
    ]


class FrameStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _path(self, collection: str, id: str) -> Path:
        validate_id(collection)
        validate_id(id)
        return self.root / collection / f"{id}.parquet"

    def save_frame(
        self, collection: str, id: str, frame: pd.DataFrame, *, overwrite: bool = True
    ) -> None:
        path = self._path(collection, id)
        if path.exists() and not overwrite:
            raise FileExistsError(f"frame already exists: {collection}/{id}")
        path.parent.mkdir(parents=True, exist_ok=True)
        frame.to_parquet(path, index=False)

    def load_frame(self, collection: str, id: str) -> pd.DataFrame | None:
        path = self._path(collection, id)
        if not path.exists():
            return None
        return pd.read_parquet(path)

    def exists(self, collection: str, id: str) -> bool:
        return self._path(collection, id).exists()

    def delete(self, collection: str, id: str) -> None:
        self._path(collection, id).unlink(missing_ok=True)


_frame_store: FrameStore | None = None


def configure_frame_store(store: FrameStore) -> None:
    """Install the process-wide frame store. App startup calls this once with a
    FrameStore under the workspace; each test installs a fresh one rooted at its
    own tmp dir."""
    global _frame_store
    _frame_store = store


def get_frame_store() -> FrameStore:
    if _frame_store is None:
        raise RuntimeError("frame store not configured; call configure_frame_store() first")
    return _frame_store


def is_frame_store_configured() -> bool:
    return _frame_store is not None

"""Parquet storage for the tabular payloads that aren't documents — run stage
outputs, review-queue snapshots, decision logs, and uploaded eval datasets. Same
(collection, id) addressing as the document store, different physical form: one
parquet file per frame under a root directory. The only place outside the
document store that turns an id into a file path, so it reuses validate_id."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.core.persistence import validate_id


class FrameStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _path(self, collection: str, id: str) -> Path:
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

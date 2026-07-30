"""input_data stage: the connector handle, its kind/format vocabularies, and the
xlsx read parameters the runtime pulls out of `connector.params`."""
from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any, ClassVar, Optional

from pydantic import ConfigDict, Field, model_validator

from app.models.schema import _Base


class ConnectorKind(str, Enum):
    file = "file"


class FileFormat(str, Enum):
    csv = "csv"
    parquet = "parquet"
    json = "json"
    geojson = "geojson"
    xlsx = "xlsx"


class Connector(_Base):
    """input_data handle."""
    # Every field changes what this stage computes (which file, what params) —
    # see Stage.compute_definition_fingerprint.
    FINGERPRINT_FIELDS: ClassVar[frozenset[str]] = frozenset({"kind", "params", "refresh", "notes"})
    INCIDENTAL_FIELDS: ClassVar[frozenset[str]] = frozenset()

    kind: ConnectorKind
    params: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Connector parameters. For kind=file: params.path, when present, is the "
            "ABSOLUTE path to the data file, plus optional params.format "
            "(csv/parquet/json/geojson/xlsx). If the source material does not state "
            "where the file lives, OMIT path entirely — the user binds a file when "
            "starting a run. Never invent a path."
        ),
    )
    refresh: str = "ad_hoc"
    notes: Optional[str] = None

    @model_validator(mode="after")
    def _params_for_kind(self) -> "Connector":
        if self.kind == ConnectorKind.file:
            path = (self.params or {}).get("path")
            if path is not None:
                if not isinstance(path, str) or not path.strip():
                    raise ValueError("connector params.path must be a non-empty string when present")
                if not Path(path).is_absolute():
                    raise ValueError(f"connector params.path must be an ABSOLUTE path, got {path!r}")
            fmt = (self.params or {}).get("format")
            if fmt is not None and fmt not in {f.value for f in FileFormat}:
                raise ValueError(f"unknown file format {fmt!r}")
        return self


class XlsxReadParams(_Base):
    # extra=ignore: callers pass the whole connector.params dict, incl. other formats' keys
    model_config = ConfigDict(strict=True, extra="ignore")

    sheet_name: str | int = 0
    header_row: int = 0
    first_column: int = 0
    source_row_column: str | None = None

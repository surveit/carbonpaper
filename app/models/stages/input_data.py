"""input_data stage: the connector block that names a source dataset, the
file-format vocabulary it validates `params.format` against, and the typed
read parameters an xlsx source is read with."""
from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any, ClassVar, Literal, Optional

from pydantic import ConfigDict, Field, model_validator

from app.models.schema import StageConfig, _Base
from app.models.stages.stage_base import StageBase, StageType
from app.models.stages.node_spec import NodeTypeSpec
from app.models.stages.signature import ReplacesSignature


class ConnectorKind(str, Enum):
    file = "file"


class FileFormat(str, Enum):
    csv = "csv"
    parquet = "parquet"
    json = "json"
    geojson = "geojson"
    xlsx = "xlsx"


class Connector(StageConfig):
    """input_data config block."""
    # Every field changes what this stage computes (which file, what params) —
    # see StageBase.compute_definition_fingerprint.
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


class InputDataStage(StageBase):
    type: Literal[StageType.input_data]
    connector: Connector
    # The root of the schema graph: no inputs, so `produces` IS the declaration
    # of what the source supplies — the degenerate replaces form.
    signature: ReplacesSignature

    def fingerprint_blocks(self) -> dict[str, StageConfig]:
        return {"connector": self.connector}


class XlsxReadParams(_Base):
    # extra=ignore: callers pass the whole connector.params dict, incl. other formats' keys
    model_config = ConfigDict(strict=True, extra="ignore")

    sheet_name: str | int = 0
    header_row: int = 0
    first_column: int = 0
    source_row_column: str | None = None

# Authoring copy for this module's stage type(s); assembled into NODE_TYPES.
NODE_TYPE_SPECS: dict[str, NodeTypeSpec] = {
    "input_data": NodeTypeSpec(
        summary="Declares a source dataset with a typed schema.",
        signature_form="replaces",
        blocks=["connector"],
        requires_inputs=False,
        min_inputs=0,
        required=["kind"],
        optional=["params", "refresh", "notes"],
        notes=(
            "When the methodology names a specific static file, params.path may "
            "carry it and MUST be an ABSOLUTE path; when the source does not say "
            "where the data lives, omit path — the user binds a file when starting "
            "a run. Never invent a path. "
            "For format=xlsx, optional params select the sheet and skip leading "
            "rows or columns: sheet_name (name or 0-based position, default first "
            "sheet), header_row (0-based index of the header row, default 0) and "
            "first_column (0-based index of the first column read, default 0). "
            "Takes no inputs; the signature's `produces` declares what the source supplies."
        ),
    ),
}

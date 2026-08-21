"""input_data stage: the connector block that names a source dataset, the
file-format vocabulary it validates `params.format` against, and the typed
read parameters an xlsx source is read with."""
from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any, ClassVar, Literal, Optional

from pydantic import ConfigDict, Field, model_validator

from app.core.source_files import FileFormat as FileFormat
from app.core.source_files import resolve_file_format as resolve_file_format
from app.models.schema import StageConfig, _Base
from app.models.stages.stage_base import AbstractStage, StageType
from app.models.stages.stage_type_spec import StageTypeSpec
from app.models.stages.signature import ReplacesSignature


class ConnectorKind(str, Enum):
    file = "file"


class Connector(StageConfig):
    FINGERPRINT_FIELDS: ClassVar[frozenset[str]] = frozenset({"kind", "params", "refresh", "notes"})
    INCIDENTAL_FIELDS: ClassVar[frozenset[str]] = frozenset()

    kind: ConnectorKind
    params: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Connector parameters. For kind=file: params.path, when present, is the "
            "ABSOLUTE path to the data file, plus optional params.format "
            "(csv/tsv/parquet/json/geojson/xlsx). If the source material does not state "
            "where the file lives, OMIT path entirely — the user binds a file when "
            "starting a run. Never invent a path. params.paths is the same thing for a "
            "source that arrives split across several files of one shape: the run reads "
            "them in order and concatenates. Never author both."
        ),
    )
    refresh: str = "ad_hoc"
    notes: Optional[str] = None

    @model_validator(mode="after")
    def _params_for_kind(self) -> "Connector":
        if self.kind == ConnectorKind.file:
            params = self.params or {}
            _refuse_both_path_forms(params)
            if params.get("path") is not None:
                _refuse_unusable_path(params["path"], "params.path")
            for position, path in enumerate(params.get("paths") or []):
                _refuse_unusable_path(path, f"params.paths[{position}]")
            fmt = params.get("format")
            if fmt is not None and fmt not in {f.value for f in FileFormat}:
                raise ValueError(f"unknown file format {fmt!r}")
        return self


def read_connector_paths(params: dict[str, Any]) -> list[str]:
    """Every file this connector reads, in order — [] when nothing is bound yet."""
    if params.get("paths") is not None:
        return list(params["paths"])
    return [params["path"]] if params.get("path") is not None else []


def _refuse_both_path_forms(params: dict[str, Any]) -> None:
    if params.get("path") is not None and params.get("paths") is not None:
        raise ValueError(
            "connector params carry both path and paths; one file goes in path, "
            "several in paths, never both")
    paths = params.get("paths")
    if paths is not None and (not isinstance(paths, list) or not paths):
        raise ValueError("connector params.paths must be a non-empty list when present")


def _refuse_unusable_path(path: Any, named: str) -> None:
    if not isinstance(path, str) or not path.strip():
        raise ValueError(f"connector {named} must be a non-empty string when present")
    if not Path(path).is_absolute():
        raise ValueError(f"connector {named} must be an ABSOLUTE path, got {path!r}")


class InputDataStage(AbstractStage):
    type: Literal[StageType.input_data]
    CACHE_IGNORED_BECAUSE: ClassVar[str] = (
        "a source reads its input afresh every run, so there is nothing to replay"
    )
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

# Authoring copy for this module's stage type(s); assembled into STAGE_TYPES.
STAGE_TYPE_SPECS: dict[str, StageTypeSpec] = {
    "input_data": StageTypeSpec(
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

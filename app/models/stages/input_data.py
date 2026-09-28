"""input_data stage: the connector block that names a source dataset, the
file-format vocabulary it validates `params.format` against, and the typed
read parameters an xlsx source is read with."""
from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, ClassVar, Literal, Optional, Union

from pydantic import (
    ConfigDict, Field, GetPydanticSchema, SerializeAsAny, StrictInt, ValidationInfo,
    WithJsonSchema, field_validator, model_validator,
)

from app.core.source_files import FileFormat as FileFormat
from app.core.source_files import resolve_file_format as resolve_file_format
from app.models.connectors import ConnectorParams, find_connector
from app.models.schema import StageConfig
from app.models.stages.stage_base import AbstractStage, StageType
from app.models.stages.stage_type_spec import StageTypeSpec
from app.models.stages.signature import ReplacesSignature


# The key a stage stored before an input could read several files carried.
LEGACY_SINGLE_PATH_KEY = "path"


class ConnectorKind(str, Enum):
    file = "file"


# `paths` are read in order and concatenated; a run binds them when the workflow names none.
class FileConnectorParams(ConnectorParams):
    model_config = ConfigDict(extra="forbid")

    format: Optional[FileFormat] = None
    # What pandas is told to read a column AS, overriding what the schema implies.
    dtype: Optional[dict[str, str]] = None
    list_columns: list[str] = Field(default_factory=list)
    parse_dates: list[str] = Field(default_factory=list)
    # xlsx only: which sheet, and how far in the table starts.
    sheet_name: Union[str, StrictInt] = 0
    # Strict: True is an int to Python, and `header_row=True` was a real authoring slip.
    header_row: StrictInt = 0
    first_column: StrictInt = 0
    source_row_column: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _fold_legacy_single_path(cls, data: Any) -> Any:
        """Stages stored before an input could read several files carry `path`; it is paths[0]."""
        if not isinstance(data, dict) or LEGACY_SINGLE_PATH_KEY not in data:
            return data
        folded = dict(data)
        legacy = folded.pop(LEGACY_SINGLE_PATH_KEY)
        if folded.get("paths"):
            raise ValueError(
                f"connector params carry both {LEGACY_SINGLE_PATH_KEY!r} and 'paths'; "
                f"{LEGACY_SINGLE_PATH_KEY!r} is what a stage stored before an input could "
                "read several files carried, and dropping either silently would change "
                "which files the run reads")
        if legacy is not None:
            folded["paths"] = [legacy]
        return folded


class Connector(StageConfig):
    FINGERPRINT_FIELDS: ClassVar[frozenset[str]] = frozenset({"kind", "params", "refresh", "notes"})
    INCIDENTAL_FIELDS: ClassVar[frozenset[str]] = frozenset()

    # The authoring schema offers the file kind alone; a pack's kind is authored by hand.
    kind: Annotated[str, WithJsonSchema({"type": "string", "enum": [ConnectorKind.file.value]})]
    params: Annotated[
        SerializeAsAny[ConnectorParams],
        GetPydanticSchema(get_pydantic_json_schema=lambda _core_schema, handler: handler(
            FileConnectorParams.__pydantic_core_schema__)),
    ] = Field(
        default_factory=FileConnectorParams,
        description=(
            "Connector parameters. For kind=file: params.paths is the list of ABSOLUTE "
            "paths to read, in order, concatenated into one table — every one of them "
            "must carry the same columns. Plus optional params.format "
            "(csv/tsv/parquet/json/geojson/xlsx). If the source material does not state "
            "where the file lives, OMIT paths entirely — the user binds files when "
            "starting a run. Never invent a path."
        ),
    )
    refresh: str = "ad_hoc"
    notes: Optional[str] = None

    @field_validator("kind")
    @classmethod
    def _kind_is_file_or_registered(cls, kind: str) -> str:
        _find_params_model(kind)
        return kind

    @field_validator("params", mode="before")
    @classmethod
    def _read_params_as_the_kinds_own(cls, params: Any, info: ValidationInfo) -> ConnectorParams:
        if "kind" not in info.data:
            raise ValueError("params are read by their connector kind's model, and no kind is valid")
        model = _find_params_model(info.data["kind"])
        # An omitted block is the file kind's empty params, which another kind reads as unset.
        if isinstance(params, ConnectorParams) and not isinstance(params, model):
            params = params.model_dump(exclude_unset=True)
        return model.model_validate(params)


def _find_params_model(kind: str) -> type[ConnectorParams]:
    return FileConnectorParams if kind == ConnectorKind.file else find_connector(kind).params_model


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

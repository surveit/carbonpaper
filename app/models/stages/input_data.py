"""input_data stage: the connector block that names a source dataset, the
file-format vocabulary it validates `params.format` against, and the typed
read parameters an xlsx source is read with."""
from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal, Optional, Union
from urllib.parse import urlparse

from pydantic import AfterValidator, ConfigDict, Field, StrictInt, model_validator

from app.core.source_files import FileFormat as FileFormat
from app.core.source_files import resolve_file_format as resolve_file_format
from app.models.schema import StageConfig, _Base
from app.models.stages.stage_base import AbstractStage, StageType
from app.models.stages.stage_type_spec import StageTypeSpec
from app.models.stages.signature import ReplacesSignature


# The key a stage stored before an input could read several files carried.
LEGACY_SINGLE_PATH_KEY = "path"


def _refuse_a_relative_path(path: str) -> str:
    if not path.strip():
        raise ValueError("a connector path must be a non-empty string")
    if not Path(path).is_absolute():
        raise ValueError(f"a connector path must be ABSOLUTE, got {path!r}")
    return path


AbsolutePath = Annotated[str, AfterValidator(_refuse_a_relative_path)]


class ConnectorKind(str, Enum):
    file = "file"
    fetch = "fetch"


# `paths` are read in order and concatenated; a run binds them when the workflow names none.
class FileConnectorParams(_Base):
    model_config = ConfigDict(extra="forbid")

    paths: list[AbsolutePath] = Field(default_factory=list)
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
    # kind=fetch only: where the bytes are published, and headers the request carries.
    url: Optional[str] = None
    headers: Optional[dict[str, str]] = None

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

    kind: ConnectorKind
    params: FileConnectorParams = Field(
        default_factory=FileConnectorParams,
        description=(
            "Connector parameters. For kind=file: params.paths is the list of ABSOLUTE "
            "paths to read, in order, concatenated into one table — every one of them "
            "must carry the same columns. Plus optional params.format "
            "(csv/tsv/parquet/json/geojson/xlsx). If the source material does not state "
            "where the file lives, OMIT paths entirely — the user binds files when "
            "starting a run. Never invent a path. "
            "For kind=fetch: params.url is the REQUIRED http(s) address the run "
            "downloads, plus params.format, which is itself required unless the URL "
            "path ends in a known extension. Never invent a URL. "
            "params.headers is an optional mapping of header name to a LITERAL string "
            "value sent with the request, for an endpoint that authenticates — e.g. "
            "{\"Authorization\": \"Token abc123\"}. That value is STORED IN THIS STAGE'S "
            "CONFIG, which is versioned and travels in a project export, so put nothing "
            "there you would not ship with the workflow. Never invent a credential."
        ),
    )
    refresh: str = "ad_hoc"
    notes: Optional[str] = None

    @model_validator(mode="after")
    def _params_for_kind(self) -> "Connector":
        if self.kind == ConnectorKind.fetch:
            _refuse_unusable_url(self.params.url, self.params.format)
            _refuse_unusable_headers(self.params.headers)
        else:
            _refuse_fetch_params_on_a_file(self.params)
        return self


def _refuse_fetch_params_on_a_file(params: FileConnectorParams) -> None:
    for name in ("url", "headers"):
        if getattr(params, name) is not None:
            raise ValueError(f"connector params.{name} is only for kind=fetch")


def _refuse_unusable_url(url: Optional[str], fmt: Optional[str]) -> None:
    """Unlike a file path there is no run-form field for a URL, so a fetch stage must carry its own."""
    if url is None or not url.strip():
        raise ValueError("connector params.url is required for kind=fetch")
    if urlparse(url).scheme not in ("http", "https"):
        raise ValueError(f"connector params.url must be http(s), got {url!r}")
    if fmt is None and _suffix_format(url) is None:
        raise ValueError(
            f"connector params.format is required: nothing in {url!r} says what it "
            "returns. An endpoint that serves csv from a path with no extension is the "
            "usual case — declare the format rather than letting the read guess."
        )


def _refuse_unusable_headers(headers: Optional[dict[str, str]]) -> None:
    """A header is foreign text put into a request: CR or LF in either half injects more."""
    for name, value in (headers or {}).items():
        if not name.strip():
            raise ValueError(f"connector params.headers has a blank name: {name!r}")
        _refuse_header_line_break(name, name)
        _refuse_header_line_break(name, value)


def _refuse_header_line_break(name: str, text: str) -> None:
    if "\r" in text or "\n" in text:
        raise ValueError(
            f"connector params.headers[{name!r}] holds a line break, which would inject "
            "further headers into the request")


def _suffix_format(url: str) -> FileFormat | None:
    try:
        return resolve_file_format(urlparse(url).path)
    except ValueError:
        return None


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
            "kind=file when the data arrives as a file, kind=fetch when the "
            "methodology names a URL the data is published at. "
            "When the methodology names a specific static file, params.path may "
            "carry it and MUST be an ABSOLUTE path; when the source does not say "
            "where the data lives, omit path — the user binds a file when starting "
            "a run. Never invent a path. "
            "kind=fetch takes params.url instead of a path, downloaded when a run "
            "is checked before it starts and held, so a re-run reads the bytes the "
            "first run read. Its "
            "params.format is required unless the URL path ends in a known "
            "extension — an endpoint like /api/grants says nothing about what it "
            "returns. Never invent a URL. "
            "An endpoint that authenticates takes params.headers, a mapping of "
            "header name to a literal string value (e.g. Authorization: Token "
            "abc123). The value is stored in the stage config, which is versioned "
            "and travels in a project export. Never invent a credential. "
            "For format=xlsx, optional params select the sheet and skip leading "
            "rows or columns: sheet_name (name or 0-based position, default first "
            "sheet), header_row (0-based index of the header row, default 0) and "
            "first_column (0-based index of the first column read, default 0). "
            "Takes no inputs; the signature's `produces` declares what the source supplies."
        ),
    ),
}

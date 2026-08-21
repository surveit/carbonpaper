"""input_data stage: the connector block that names a source dataset, the
file-format vocabulary it validates `params.format` against, and the typed
read parameters an xlsx source is read with."""
from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any, ClassVar, Literal, Optional
from urllib.parse import urlparse

from pydantic import ConfigDict, Field, model_validator

from app.core.source_files import FileFormat as FileFormat
from app.core.source_files import resolve_file_format as resolve_file_format
from app.models.schema import StageConfig, _Base
from app.models.stages.stage_base import AbstractStage, StageType
from app.models.stages.stage_type_spec import StageTypeSpec
from app.models.stages.signature import ReplacesSignature


class ConnectorKind(str, Enum):
    file = "file"
    fetch = "fetch"


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
        params = self.params or {}
        fmt = params.get("format")
        if fmt is not None and fmt not in {f.value for f in FileFormat}:
            raise ValueError(f"unknown file format {fmt!r}")
        if self.kind == ConnectorKind.file:
            _refuse_unusable_path(params.get("path"))
        if self.kind == ConnectorKind.fetch:
            _refuse_unusable_url(params.get("url"), fmt)
            _refuse_unusable_headers(params.get("headers"))
        return self


def _refuse_unusable_path(path: Any) -> None:
    """Absent is fine — the run form binds it. Present and relative is not."""
    if path is None:
        return
    if not isinstance(path, str) or not path.strip():
        raise ValueError("connector params.path must be a non-empty string when present")
    if not Path(path).is_absolute():
        raise ValueError(f"connector params.path must be an ABSOLUTE path, got {path!r}")


def _refuse_unusable_url(url: Any, fmt: Any) -> None:
    """Unlike a file path there is no run-form field for a URL, so a fetch stage must carry its own."""
    if not isinstance(url, str) or not url.strip():
        raise ValueError("connector params.url is required for kind=fetch")
    if urlparse(url).scheme not in ("http", "https"):
        raise ValueError(f"connector params.url must be http(s), got {url!r}")
    if fmt is None and _suffix_format(url) is None:
        raise ValueError(
            f"connector params.format is required: nothing in {url!r} says what it "
            "returns. An endpoint that serves csv from a path with no extension is the "
            "usual case — declare the format rather than letting the read guess."
        )


def _refuse_unusable_headers(headers: Any) -> None:
    """A header is foreign text put into a request: CR or LF in either half injects more."""
    if headers is None:
        return
    if not isinstance(headers, dict):
        raise ValueError(
            f"connector params.headers must be a mapping of header name to value, got "
            f"{type(headers).__name__}")
    for name, value in headers.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"connector params.headers has a blank or non-text name: {name!r}")
        if not isinstance(value, str):
            raise ValueError(
                f"connector params.headers[{name!r}] must be a literal string value, got "
                f"{type(value).__name__}")
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
            "kind=file when the data arrives as a file, kind=fetch when the "
            "methodology names a URL the data is published at. "
            "When the methodology names a specific static file, params.path may "
            "carry it and MUST be an ABSOLUTE path; when the source does not say "
            "where the data lives, omit path — the user binds a file when starting "
            "a run. Never invent a path. "
            "kind=fetch takes params.url instead, downloaded when a run is prepared "
            "and held, so a re-run reads the bytes the first run read. Its "
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

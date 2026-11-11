"""recap_files: named RECAP archive files, each with the case facts its workflow states for it."""
from __future__ import annotations

from typing import Annotated, Iterator, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.core.url_fetch import open_url
from app.models.connectors import (
    AcquiredBytes, ConnectorParams, ConnectorSpec, MetadataValue, MirroredBytes,
)
from app.models.schema import Column
from app.packs.docket.connector import (
    RECAP_STORAGE, USER_AGENT, EcfNumber, name_recap_file, parse_ecf_number,
)
from app.packs.docket.mirror import Mirror, resolve_mirror_dir

_TIMEOUT_S = 60

# A file in its case's directory, or at the older flat path <case>.<entry>.<attachment>.pdf.
RecapPath = Annotated[str, StringConstraints(
    pattern=r"^recap/gov\.uscourts\.[a-z0-9]+\.[0-9]+(?:/[^/]+|\.[0-9]+\.[0-9]+)\.pdf$")]


class RecapFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The file's path on storage.courtlistener.com, as the docket's download link gives it.
    path: RecapPath
    court: Annotated[str, StringConstraints(pattern=r"^[a-z0-9]+$")]
    case_number: Annotated[str, StringConstraints(pattern=r"^[0-9A-Za-z:-]+$")]
    case_name: Annotated[str, StringConstraints(min_length=1)]
    docket_id: int = Field(gt=0)
    ecf: EcfNumber
    # Where the file sits among its case's files, counting from 1.
    part: int = Field(ge=1)
    description: Annotated[str, StringConstraints(min_length=1)]


class RecapFilesParams(ConnectorParams):
    files: list[RecapFile] = Field(min_length=1)
    # A mirror read in place of CourtListener; a relative path is a directory of this checkout.
    cache_dir: Annotated[str, StringConstraints(min_length=1)] | None = None

    @model_validator(mode="after")
    def _names_each_file_once(self) -> Self:
        paths = [file.path for file in self.files]
        repeated = sorted({path for path in paths if paths.count(path) > 1})
        if repeated:
            raise ValueError(f"files names {repeated} more than once")
        parts = [(file.court, file.case_number, file.part) for file in self.files]
        clashing = sorted({part for part in parts if parts.count(part) > 1})
        if clashing:
            raise ValueError(f"files gives one case two files at the same part: {clashing}")
        return self


def acquire_recap_files(params: RecapFilesParams) -> Iterator[AcquiredBytes]:
    mirror = Mirror.load(resolve_mirror_dir(params.cache_dir)) if params.cache_dir else None
    for file in params.files:
        yield _acquire_file(file, mirror)


def _acquire_file(file: RecapFile, mirror: Mirror | None) -> AcquiredBytes:
    origin_url = f"{RECAP_STORAGE}{file.path}"
    filename = name_recap_file(file.court, file.case_number, file.ecf)
    metadata = _read_metadata(file)
    if mirror is None:
        return AcquiredBytes(
            filename, origin_url,
            lambda: open_url(origin_url, user_agent=USER_AGENT, timeout_s=_TIMEOUT_S), metadata)
    path, record = mirror.find(origin_url)
    return MirroredBytes(filename, origin_url, lambda: path.open("rb"), metadata,
                         fetched_at=record.fetched_at, sha256=record.sha256)


def _read_metadata(file: RecapFile) -> dict[str, MetadataValue]:
    entry, attachment = parse_ecf_number(file.ecf)
    return {"case_name": file.case_name, "court": file.court, "case_number": file.case_number,
            "docket_id": file.docket_id, "ecf_entry": entry, "attachment": attachment,
            "part": file.part, "description": file.description}


_METADATA_COLUMNS = (
    Column(name="case_name", type="str", nullable=True),
    Column(name="court", type="str", nullable=True),
    Column(name="case_number", type="str", nullable=True),
    Column(name="docket_id", type="int", nullable=True),
    Column(name="ecf_entry", type="int", nullable=True),
    Column(name="attachment", type="int", nullable=True),
    Column(name="part", type="int", nullable=True),
    Column(name="description", type="str", nullable=True),
)

RECAP_FILES = ConnectorSpec(
    kind="recap_files", params_model=RecapFilesParams, metadata_columns=_METADATA_COLUMNS,
    acquire=acquire_recap_files)

"""recap_docket: a docket's filings as CourtListener's RECAP archive holds them, one row per file."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Annotated, BinaryIO, Iterator, Self
from urllib.error import URLError
from urllib.request import Request, urlopen

from pydantic import (
    AliasChoices, BaseModel, ConfigDict, Field, StringConstraints, ValidationError,
    model_validator,
)

from app.core.errors import MirrorDisagrees, SourceUnavailable
from app.core.files import compute_sha256
from app.models.connectors import (
    AbsolutePath, AcquiredBytes, ConnectorParams, ConnectorSpec, MetadataValue, MirroredBytes,
)
from app.models.schema import Column

COURTLISTENER_API = "https://www.courtlistener.com/api/rest/v4/"
RECAP_STORAGE = "https://storage.courtlistener.com/"
USER_AGENT = "carbonpaper (https://github.com/surveit/carbonpaper)"
MIRROR_MANIFEST = "manifest.jsonl"
_TIMEOUT_S = 60

EcfNumber = Annotated[str, StringConstraints(pattern=r"^[1-9][0-9]*(-[1-9][0-9]*)?$")]


class RecapDocketParams(ConnectorParams):
    docket_id: int = Field(gt=0)
    # As the docket prints them: "58" is document 58 itself, "221-1" its attachment 1.
    entries: list[EcfNumber] = Field(min_length=1)
    court: Annotated[str, StringConstraints(pattern=r"^[a-z0-9]+$")]
    case_number: Annotated[str, StringConstraints(pattern=r"^[0-9A-Za-z:-]+$")]
    # A mirror read in place of CourtListener; its manifest.jsonl records every file it holds.
    cache_dir: AbsolutePath | None = None

    @model_validator(mode="after")
    def _names_each_entry_once(self) -> Self:
        repeated = sorted({number for number in self.entries if self.entries.count(number) > 1})
        if repeated:
            raise ValueError(f"entries names {repeated} more than once")
        return self


def acquire_recap_docket(params: RecapDocketParams) -> Iterator[AcquiredBytes]:
    mirror = _Mirror.load(Path(params.cache_dir)) if params.cache_dir is not None else None
    listing = _read_listing(params.docket_id, mirror)
    for number, document in _find_requested_documents(listing, params):
        yield _acquire_document(params, number, document, mirror)


class _RecapDocument(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    entry_number: int | None
    attachment_number: int | None
    entry_date_filed: date | None
    description: str | None
    page_count: int | None
    pacer_doc_id: str | None
    filepath_local: str | None
    is_available: bool


class _SearchPage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    next: str | None
    results: list[_RecapDocument]


class _MirrorRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    path: str
    origin_url: str | None = Field(validation_alias=AliasChoices("source_url", "origin_url"))
    fetched_at: str
    sha256: str


@dataclass(frozen=True)
class _Mirror:
    root: Path
    records_by_url: dict[str, list[_MirrorRecord]]

    @classmethod
    def load(cls, root: Path) -> _Mirror:
        manifest = root / MIRROR_MANIFEST
        if not manifest.is_file():
            raise SourceUnavailable(
                f"no {MIRROR_MANIFEST} in {root}: a mirror must record where and when it "
                "fetched each file it holds")
        records_by_url: dict[str, list[_MirrorRecord]] = defaultdict(list)
        lines = manifest.read_text(encoding="utf-8").splitlines()
        for record in _parse_mirror_records(manifest, lines):
            if record.origin_url is not None:
                records_by_url[record.origin_url].append(record)
        return cls(root, dict(records_by_url))

    def find(self, url: str) -> tuple[Path, _MirrorRecord]:
        records = self.records_by_url.get(url, [])
        if len(records) != 1:
            raise SourceUnavailable(
                f"the mirror at {self.root} records {url} {len(records)} times, not once")
        path = Path(records[0].path)
        stored = path if path.is_absolute() else self.root / path
        if not stored.is_file():
            raise SourceUnavailable(f"the mirror records {url} at {stored}, which is not a file")
        return stored, records[0]

    def read(self, url: str) -> bytes:
        path, record = self.find(url)
        digest = compute_sha256(path)
        if digest != record.sha256:
            raise MirrorDisagrees(
                name=str(path), digest=digest, recorded=record.sha256, origin_url=url)
        return path.read_bytes()


def _parse_mirror_records(manifest: Path, lines: list[str]) -> list[_MirrorRecord]:
    try:
        return [_MirrorRecord.model_validate_json(line) for line in lines if line.strip()]
    except ValidationError as invalid:
        raise SourceUnavailable(
            f"{manifest} holds a line that is not a fetch record: {invalid}") from invalid


def _read_listing(docket_id: int, mirror: _Mirror | None) -> list[_RecapDocument]:
    url: str | None = (f"{COURTLISTENER_API}search/?type=rd&q=docket_id%3A{docket_id}"
                       "&order_by=entry_date_filed%20asc")
    documents: list[_RecapDocument] = []
    while url is not None:
        page = _SearchPage.model_validate_json(
            mirror.read(url) if mirror is not None else _get(url))
        documents.extend(page.results)
        url = page.next
        if url is not None and not url.startswith(COURTLISTENER_API):
            raise SourceUnavailable(
                f"docket {docket_id}'s listing points its next page at {url}, off "
                f"{COURTLISTENER_API}; it is not followed")
    return documents


def _find_requested_documents(
    listing: list[_RecapDocument], params: RecapDocketParams,
) -> list[tuple[str, _RecapDocument]]:
    found: list[tuple[str, _RecapDocument]] = []
    problems: list[str] = []
    for number in params.entries:
        matches = [document for document in listing
                   if (document.entry_number, document.attachment_number) == _parse(number)]
        if len(matches) != 1:
            problems.append(f"ECF {number} matches {len(matches)} documents on docket "
                            f"{params.docket_id}'s RECAP listing, not one")
        elif not matches[0].is_available or not matches[0].filepath_local:
            problems.append(f"ECF {number} is on the docket but not in the RECAP archive; "
                            "buy it on PACER, or take it out of params.entries")
        else:
            found.append((number, matches[0]))
    if problems:
        raise SourceUnavailable("; ".join(problems))
    return found


def _acquire_document(
    params: RecapDocketParams, number: str, document: _RecapDocument, mirror: _Mirror | None,
) -> AcquiredBytes:
    origin_url = f"{RECAP_STORAGE}{document.filepath_local}"
    filename = _name_file(params, number)
    metadata = _read_metadata(number, document)
    if mirror is None:
        return AcquiredBytes(filename, origin_url, lambda: _open_url(origin_url), metadata)
    path, record = mirror.find(origin_url)
    return MirroredBytes(filename, origin_url, lambda: path.open("rb"), metadata,
                         fetched_at=record.fetched_at, sha256=record.sha256)


def _name_file(params: RecapDocketParams, number: str) -> str:
    """ECF 221-1 of txnd case 4:21-cr-00005 is txnd-4-21-cr-00005_ecf0221-att1.pdf."""
    entry, attachment = _parse(number)
    suffix = f"-att{attachment}" if attachment is not None else ""
    return f"{params.court}-{params.case_number.replace(':', '-')}_ecf{entry:04d}{suffix}.pdf"


def _read_metadata(number: str, document: _RecapDocument) -> dict[str, MetadataValue]:
    entry, attachment = _parse(number)
    return {"ecf_entry": entry, "attachment": attachment,
            "date_filed": document.entry_date_filed, "description": document.description,
            "page_count": document.page_count, "pacer_doc_id": document.pacer_doc_id,
            "courtlistener_id": document.id}


def _parse(number: str) -> tuple[int, int | None]:
    entry, _, attachment = number.partition("-")
    return int(entry), int(attachment) if attachment else None


def _get(url: str) -> bytes:
    with _open_url(url) as response:
        return response.read()


def _open_url(url: str) -> BinaryIO:
    try:
        response: BinaryIO = urlopen(
            Request(url, headers={"User-Agent": USER_AGENT}), timeout=_TIMEOUT_S)
    except (URLError, TimeoutError) as failure:
        raise SourceUnavailable(f"GET {url} failed: {failure}") from failure
    return response


_METADATA_COLUMNS = (
    Column(name="ecf_entry", type="int", nullable=True),
    Column(name="attachment", type="int", nullable=True),
    Column(name="date_filed", type="date", nullable=True),
    Column(name="description", type="str", nullable=True),
    Column(name="page_count", type="int", nullable=True),
    Column(name="pacer_doc_id", type="str", nullable=True),
    Column(name="courtlistener_id", type="int", nullable=True),
)

RECAP_DOCKET = ConnectorSpec(
    kind="recap_docket", params_model=RecapDocketParams, metadata_columns=_METADATA_COLUMNS,
    acquire=acquire_recap_docket)

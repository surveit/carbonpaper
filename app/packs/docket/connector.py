"""recap_docket: a docket's filings as CourtListener's RECAP archive holds them, one row per file."""
from __future__ import annotations

from datetime import date
from typing import Annotated, Iterator, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.core.errors import SourceUnavailable
from app.core.url_fetch import open_url, read_url
from app.models.connectors import (
    AcquiredBytes, ConnectorParams, ConnectorSpec, MetadataValue, MirroredBytes,
)
from app.models.schema import Column
from app.packs.docket.mirror import MIRROR_MANIFEST as MIRROR_MANIFEST
from app.packs.docket.mirror import Mirror, resolve_mirror_dir

COURTLISTENER_API = "https://www.courtlistener.com/api/rest/v4/"
RECAP_STORAGE = "https://storage.courtlistener.com/"
USER_AGENT = "carbonpaper (https://github.com/surveit/carbonpaper)"
_TIMEOUT_S = 60

EcfNumber = Annotated[str, StringConstraints(pattern=r"^[1-9][0-9]*(-[1-9][0-9]*)?$")]


class RecapDocketParams(ConnectorParams):
    docket_id: int = Field(gt=0)
    # As the docket prints them: "58" is document 58 itself, "221-1" its attachment 1.
    entries: list[EcfNumber] = Field(min_length=1)
    court: Annotated[str, StringConstraints(pattern=r"^[a-z0-9]+$")]
    case_number: Annotated[str, StringConstraints(pattern=r"^[0-9A-Za-z:-]+$")]
    # A mirror read in place of CourtListener; a relative path is a directory of this checkout.
    cache_dir: Annotated[str, StringConstraints(min_length=1)] | None = None

    @model_validator(mode="after")
    def _names_each_entry_once(self) -> Self:
        repeated = sorted({number for number in self.entries if self.entries.count(number) > 1})
        if repeated:
            raise ValueError(f"entries names {repeated} more than once")
        return self


def acquire_recap_docket(params: RecapDocketParams) -> Iterator[AcquiredBytes]:
    mirror = Mirror.load(resolve_mirror_dir(params.cache_dir)) if params.cache_dir else None
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


def _read_listing(docket_id: int, mirror: Mirror | None) -> list[_RecapDocument]:
    url: str | None = (f"{COURTLISTENER_API}search/?type=rd&q=docket_id%3A{docket_id}"
                       "&order_by=entry_date_filed%20asc")
    documents: list[_RecapDocument] = []
    while url is not None:
        page = _SearchPage.model_validate_json(
            mirror.read(url) if mirror is not None
            else read_url(url, user_agent=USER_AGENT, timeout_s=_TIMEOUT_S))
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
                   if (document.entry_number, document.attachment_number) == parse_ecf_number(number)]
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
    params: RecapDocketParams, number: str, document: _RecapDocument, mirror: Mirror | None,
) -> AcquiredBytes:
    origin_url = f"{RECAP_STORAGE}{document.filepath_local}"
    filename = name_recap_file(params.court, params.case_number, number)
    metadata = _read_metadata(number, document)
    if mirror is None:
        return AcquiredBytes(
            filename, origin_url,
            lambda: open_url(origin_url, user_agent=USER_AGENT, timeout_s=_TIMEOUT_S), metadata)
    path, record = mirror.find(origin_url)
    return MirroredBytes(filename, origin_url, lambda: path.open("rb"), metadata,
                         fetched_at=record.fetched_at, sha256=record.sha256)


def name_recap_file(court: str, case_number: str, number: str) -> str:
    """ECF 221-1 of txnd case 4:21-cr-00005 is txnd-4-21-cr-00005_ecf0221-att1.pdf."""
    entry, attachment = parse_ecf_number(number)
    suffix = f"-att{attachment}" if attachment is not None else ""
    return f"{court}-{case_number.replace(':', '-')}_ecf{entry:04d}{suffix}.pdf"


def _read_metadata(number: str, document: _RecapDocument) -> dict[str, MetadataValue]:
    entry, attachment = parse_ecf_number(number)
    return {"ecf_entry": entry, "attachment": attachment,
            "date_filed": document.entry_date_filed, "description": document.description,
            "page_count": document.page_count, "pacer_doc_id": document.pacer_doc_id,
            "courtlistener_id": document.id}


def parse_ecf_number(number: str) -> tuple[int, int | None]:
    entry, _, attachment = number.partition("-")
    return int(entry), int(attachment) if attachment else None


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

import json
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

from pydantic import BaseModel, Field

API_ROOT = "https://www.federalregister.gov/api/v1/documents.json"

FIELDS = (
    "document_number",
    "type",
    "publication_date",
    "title",
    "full_text_xml_url",
    "html_url",
    "page_length",
    "docket_ids",
    "regulation_id_numbers",
)


class FederalRegisterDocument(BaseModel):
    document_number: str
    type: str
    publication_date: str
    title: str
    html_url: str
    full_text_xml_url: str | None = None
    page_length: int | None = None
    docket_ids: list[str] = Field(default_factory=list)
    regulation_id_numbers: list[str] = Field(default_factory=list)


def fetch_docket_documents(docket_id: str) -> list[FederalRegisterDocument]:
    query = [
        ("conditions[docket_id]", docket_id),
        ("per_page", "100"),
        ("order", "oldest"),
    ]
    query += [("fields[]", name) for name in FIELDS]
    payload = _get_json(f"{API_ROOT}?{urllib.parse.urlencode(query)}")
    results = payload.get("results")
    if not results:
        raise LookupError(f"Federal Register has no documents for docket {docket_id}")
    return [FederalRegisterDocument.model_validate(row) for row in results]


def fetch_document_xml(
    document: FederalRegisterDocument, cache: Path | None = None
) -> Path:
    """The XML carries REGTEXT title/part attributes that the plain text drops entirely."""
    if document.full_text_xml_url is None:
        raise LookupError(
            f"FR document {document.document_number} publishes no XML; only the XML carries "
            "the CFR title and part needed to key a provision"
        )
    target = _cache_path(document, cache)
    if target.exists() and target.stat().st_size > 0:
        return target
    with urllib.request.urlopen(document.full_text_xml_url, timeout=180) as response:
        body = response.read()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    return target


def _cache_path(document: FederalRegisterDocument, cache: Path | None) -> Path:
    directory = (
        cache if cache is not None else Path(tempfile.gettempdir()) / "docket-fr-xml"
    )
    return directory / f"{document.document_number}.xml"


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=90) as response:
        return json.loads(response.read().decode("utf-8"))

"""recap_docket against a local server and a local mirror, never against CourtListener."""
from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Iterator
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from pydantic import ValidationError

from app.core.errors import MissingInputBindingError
from app.core.files import list_project_files
from app.core.frames import read_frame_table
from app.models.connectors import SOURCE_COLUMNS
from app.models.run_manifest import index_bound_sources
from app.models.spans import Span
from app.packs.docket import connector
from app.packs.docket.connector import RecapDocketParams
from app.runtime.runner import execute_run
from app.runtime.spans import SourceTextCache, verify_span
from conftest import pinned_stages, reads_of
from pdf_fixture import write_text_pdf
from stage_seed import set_stages, save_version

_FIXTURES = Path(__file__).parent.parent / "fixtures"
# Docket 29089563's type=rd listing rows for ECF 58, 221-1, 221 and 245-1, as served.
_LISTING_ROWS = json.loads(
    (_FIXTURES / "courtlistener_rd_docket_29089563_rows.json").read_text(encoding="utf-8"))
ECF_58_PAGE_1 = _FIXTURES / "us_v_boeing_ecf58_page1.pdf"
ECF_58_PATH = "recap/gov.uscourts.txnd.342881/gov.uscourts.txnd.342881.58.0.pdf"
ECF_221_1_PATH = "recap/gov.uscourts.txnd.342881/gov.uscourts.txnd.342881.221.1_1.pdf"
ATTACHMENT_TEXT = "A test page standing in for ECF 221-1."
MIRROR_FETCHED_AT = "2026-09-28T09:15:16Z"
_SEARCH_PATH = "/api/rest/v4/search/"


class _Docket:
    """What the local server holds, and every request it was sent."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.requests: list[tuple[str, str]] = []
        self.base = ""
        self.next_page = ""

    def answer(self, target: str) -> bytes | None:
        parts = urlsplit(target)
        if parts.path == _SEARCH_PATH:
            return self._search_page(second="cursor=2" in parts.query)
        return self.files.get(parts.path.lstrip("/"))

    def _search_page(self, second: bool) -> bytes:
        rows = _LISTING_ROWS[2:] if second else _LISTING_ROWS[:2]
        following = None if second else self.next_page
        return json.dumps({"count": len(_LISTING_ROWS), "next": following,
                           "previous": None, "results": rows}).encode("utf-8")


@pytest.fixture
def docket(tmp_path: Path, monkeypatch) -> Iterator[_Docket]:
    served = _Docket({
        ECF_58_PATH: ECF_58_PAGE_1.read_bytes(),
        ECF_221_1_PATH: write_text_pdf(tmp_path / "att.pdf", [ATTACHMENT_TEXT]).read_bytes(),
    })

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            served.requests.append((self.path, self.headers.get("User-Agent", "")))
            body = served.answer(self.path)
            self.send_response(404 if body is None else 200)
            self.end_headers()
            self.wfile.write(body or b"")

        def log_message(self, format: str, *args: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    served.base = f"http://127.0.0.1:{server.server_port}"
    served.next_page = f"{served.base}{_SEARCH_PATH}?cursor=2&type=rd"
    monkeypatch.setattr(connector, "COURTLISTENER_API", f"{served.base}/api/rest/v4/")
    monkeypatch.setattr(connector, "RECAP_STORAGE", f"{served.base}/")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield served
    server.shutdown()
    server.server_close()


def _params(entries: list[str], **extra: object) -> dict[str, object]:
    return {"docket_id": 29089563, "entries": entries, "court": "txnd",
            "case_number": "4:21-cr-00005", **extra}


_METADATA = [column.model_dump(mode="json", exclude_defaults=True)
             for column in connector.RECAP_DOCKET.metadata_columns]
_SOURCE = [column.model_dump(mode="json", exclude_defaults=True) for column in SOURCE_COLUMNS]
_PAGES = [{"name": "page", "type": "int", "nullable": False},
          {"name": "page_text", "type": "str", "nullable": False},
          {"name": "page_span", "type": "span", "nullable": False}]
_READ = [column for column in _SOURCE if column["name"] in ("source_id", "source_sha256")]


def _run_filings(tmp_path: Path, params: dict[str, object], produces: list[dict] | None = None,
                 carry: list[str] | None = None, bindings: dict | None = None) -> dict:
    kept = produces if produces is not None else [*_SOURCE, *_METADATA]
    carried = carry if carry is not None else ["ecf_entry"]
    carried_columns = [column for column in kept if column["name"] in carried]
    set_stages(tmp_path, [
        {"id": "filings", "description": "Read the filings", "type": "input_data",
         "row_type_id": "filing", "connector": {"kind": "recap_docket", "params": params},
         "signature": {"form": "replaces", "produces": kept}},
        {"id": "filing_pages", "description": "Read each filing a page at a time",
         "type": "read_pages", "inputs": [{"id": "filings"}], "row_type_id": "filing_page",
         "signature": {"form": "replaces", "reads": reads_of("filings", [*_READ, *carried_columns]),
                       "produces": [*_READ, *carried_columns, *_PAGES]},
         "read_pages": {"carry": carried}},
    ])
    save_version(tmp_path.name, message="seed")
    return execute_run(tmp_path / "runs", tmp_path.name, *pinned_stages(tmp_path),
                       bindings=bindings)


def _read_output(tmp_path: Path, manifest: dict, stage_id: str) -> list[dict]:
    return read_frame_table(
        tmp_path / "runs" / manifest["run_id"] / "outputs" / f"{stage_id}.parquet").to_pylist()


def _verify_page_spans(manifest: dict, pages: list[dict]) -> None:
    bound = index_bound_sources(manifest["input_bindings"])
    texts = SourceTextCache()
    for page in pages:
        verify_span(Span.model_validate(page["page_span"]), bound, texts)


# ── a run over the live connector ────────────────────────────────────────────

def test_a_run_stores_each_filing_and_reads_its_pages_into_verified_spans(
    docket, tmp_path,
) -> None:
    manifest = _run_filings(tmp_path, _params(["58", "221-1"]))

    assert manifest["status"] == "ok"
    stored = {record.filename: record for record in list_project_files(tmp_path.name)}
    assert {name: record.origin_url for name, record in stored.items()} == {
        "txnd-4-21-cr-00005_ecf0058.pdf": f"{docket.base}/{ECF_58_PATH}",
        "txnd-4-21-cr-00005_ecf0221-att1.pdf": f"{docket.base}/{ECF_221_1_PATH}"}
    filings = _read_output(tmp_path, manifest, "filings")
    ecf_58 = stored["txnd-4-21-cr-00005_ecf0058.pdf"]
    assert filings[0] == {
        "source_id": ecf_58.id, "source_sha256": ecf_58.sha256,
        "filename": "txnd-4-21-cr-00005_ecf0058.pdf", "origin_url": ecf_58.origin_url,
        "fetched_at": ecf_58.fetched_at, "ecf_entry": 58, "attachment": None,
        "date_filed": date(2022, 2, 8), "description": _LISTING_ROWS[0]["description"],
        "page_count": 26, "pacer_doc_id": "177014896771", "courtlistener_id": 192502000}
    assert [(row["ecf_entry"], row["attachment"]) for row in filings] == [(58, None), (221, 1)]
    pages = _read_output(tmp_path, manifest, "filing_pages")
    assert [(page["ecf_entry"], page["page"]) for page in pages] == [(58, 1), (221, 1)]
    assert pages[1]["page_text"] == ATTACHMENT_TEXT
    _verify_page_spans(manifest, pages)


def test_every_request_follows_the_listing_and_names_the_app(docket, tmp_path) -> None:
    _run_filings(tmp_path, _params(["58"]))

    assert [urlsplit(path).path for path, _agent in docket.requests] == [
        _SEARCH_PATH, _SEARCH_PATH, f"/{ECF_58_PATH}"]
    assert {agent for _path, agent in docket.requests} == {connector.USER_AGENT}


def test_a_bound_stored_filing_wins_and_its_pages_verify(docket, tmp_path) -> None:
    first = _run_filings(tmp_path, _params(["58"]))
    stored_path = first["input_bindings"]["filings"]["files"][0]["path"]
    docket.requests.clear()

    manifest = _run_filings(tmp_path, _params(["58"]), produces=_SOURCE, carry=[],
                            bindings={"filings": {"paths": [stored_path]}})

    assert manifest["status"] == "ok" and docket.requests == []
    _verify_page_spans(manifest, _read_output(tmp_path, manifest, "filing_pages"))


# ── what the connector refuses rather than guesses ──────────────────────────

def test_a_filing_the_archive_lacks_is_reported_before_any_is_fetched(docket, tmp_path) -> None:
    with pytest.raises(MissingInputBindingError,
                       match="ECF 245-1 is on the docket but not in the RECAP archive"):
        _run_filings(tmp_path, _params(["58", "245-1"]))
    assert [urlsplit(path).path for path, _agent in docket.requests] == [_SEARCH_PATH] * 2
    assert list_project_files(tmp_path.name) == []


def test_an_entry_the_listing_does_not_hold_is_reported(docket, tmp_path) -> None:
    with pytest.raises(MissingInputBindingError, match="ECF 999 matches 0 documents"):
        _run_filings(tmp_path, _params(["999"]))


def test_a_next_page_off_courtlistener_is_not_followed(docket, tmp_path) -> None:
    docket.next_page = f"http://example.org{_SEARCH_PATH}?cursor=2&type=rd"
    with pytest.raises(MissingInputBindingError, match="next page at http://example.org"):
        _run_filings(tmp_path, _params(["58"]))
    assert [urlsplit(path).path for path, _agent in docket.requests] == [_SEARCH_PATH]


def test_a_request_that_fails_is_reported_with_its_url(docket, tmp_path) -> None:
    del docket.files[ECF_221_1_PATH]
    with pytest.raises(MissingInputBindingError, match=f"GET {docket.base}/{ECF_221_1_PATH}"):
        _run_filings(tmp_path, _params(["221-1"]))


@pytest.mark.parametrize(("entries", "refusal"), [
    (["58a"], "should match pattern"),
    (["58", "58"], "more than once"),
    ([], "at least 1 item"),
])
def test_entries_are_ecf_numbers_each_named_once(entries: list[str], refusal: str) -> None:
    with pytest.raises(ValidationError, match=refusal):
        RecapDocketParams.model_validate(_params(entries))


# ── a mirror in place of CourtListener ──────────────────────────────────────

@pytest.fixture
def offline(monkeypatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("a mirror read reached for the network")

    monkeypatch.setattr(connector, "urlopen", refuse)


def _read_instant(stored_stamp: str | None) -> datetime:
    """A stored stamp is naive local, the form now_iso writes."""
    assert stored_stamp is not None
    moment = datetime.fromisoformat(stored_stamp)
    assert moment.tzinfo is None
    return moment.astimezone()


def _write_mirror(root: Path, recorded_sha256: dict[str, str] | None = None) -> Path:
    root.mkdir()
    listing = root / "listing.json"
    listing.write_text(json.dumps({"next": None, "results": _LISTING_ROWS}), encoding="utf-8")
    pdf = root / "ecf0058.pdf"
    pdf.write_bytes(ECF_58_PAGE_1.read_bytes())
    listing_url = (f"{connector.COURTLISTENER_API}search/?type=rd&q=docket_id%3A29089563"
                   "&order_by=entry_date_filed%20asc")
    lines = [
        {"path": "listing.json", "source_url": listing_url, "fetched_at": "2026-09-28T09:10:16Z",
         "sha256": hashlib.sha256(listing.read_bytes()).hexdigest()},
        {"path": "ecf0058.pdf", "source_url": f"{connector.RECAP_STORAGE}{ECF_58_PATH}",
         "fetched_at": MIRROR_FETCHED_AT,
         "sha256": hashlib.sha256(pdf.read_bytes()).hexdigest()},
    ]
    for line in lines:
        line["sha256"] = (recorded_sha256 or {}).get(line["path"], line["sha256"])
    (root / connector.MIRROR_MANIFEST).write_text(
        "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    return root


def test_a_mirror_supplies_the_bytes_and_the_time_they_were_fetched(offline, tmp_path) -> None:
    mirror = _write_mirror(tmp_path / "mirror")

    manifest = _run_filings(tmp_path, _params(["58"], cache_dir=str(mirror)))

    assert manifest["status"] == "ok"
    (record,) = list_project_files(tmp_path.name)
    assert record.origin_url == f"https://storage.courtlistener.com/{ECF_58_PATH}"
    assert _read_instant(record.fetched_at) == datetime.fromisoformat(MIRROR_FETCHED_AT)
    assert record.created_at != record.fetched_at
    _verify_page_spans(manifest, _read_output(tmp_path, manifest, "filing_pages"))


def test_a_mirrored_file_off_its_recorded_sha256_is_refused(offline, tmp_path) -> None:
    mirror = _write_mirror(tmp_path / "mirror", recorded_sha256={"ecf0058.pdf": "0" * 64})
    with pytest.raises(MissingInputBindingError, match=f"its mirror recorded {'0' * 64}"):
        _run_filings(tmp_path, _params(["58"], cache_dir=str(mirror)))
    assert list_project_files(tmp_path.name) == []


def test_a_file_the_mirror_does_not_record_is_refused(offline, tmp_path) -> None:
    mirror = _write_mirror(tmp_path / "mirror")
    with pytest.raises(MissingInputBindingError, match="records .*221.1_1.pdf 0 times"):
        _run_filings(tmp_path, _params(["221-1"], cache_dir=str(mirror)))

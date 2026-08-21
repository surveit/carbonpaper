"""The fetch connector: what the model refuses at authoring time, and what the runtime
does with a URL — served by a local http server, never the public internet."""
from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from functools import partial
from http.server import BaseHTTPRequestHandler, HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

import pytest

from app.core.errors import SourceFetchError
from app.core.fetched_sources import find_fetched_file, resolve_fetched_path
from app.core.files import UploadedFile
from app.models.stages.input_data import Connector, ConnectorKind, InputDataStage
from app.models.workflow import Workflow
from app.runtime.stages.input_data import resolve_source_path

# One EA Funds grant, as their /api/grants endpoint serves it.
_GRANTS_CSV = (
    "id,fund,grantee,amount,year\n"
    "rec6rcTbCtpvrkGF3,Long-Term Future Fund,Katherine (Katie) Dammer,10000,2026\n"
)
_OTHER_CSV = "id,fund,grantee,amount,year\nrec9,Animal Welfare Fund,Someone,1,2026\n"


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return


class _EchoHeadersHandler(BaseHTTPRequestHandler):
    """Answers with the request headers it received, so a test reads what really went out."""

    def do_GET(self) -> None:
        payload = "".join(f"{name}: {value}\n" for name, value in self.headers.items())
        body = payload.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def _serve(handler: Callable[..., BaseHTTPRequestHandler]) -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def served(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    """A real http server, so the fetch runs over a socket rather than against a mock."""
    root = tmp_path / "served"
    root.mkdir()
    for base in _serve(partial(_QuietHandler, directory=str(root))):
        yield base, root


@pytest.fixture
def echoing() -> Iterator[str]:
    yield from _serve(_EchoHeadersHandler)


def _fetch_stage(stage_id: str, url: str) -> dict[str, object]:
    return {
        "id": stage_id,
        "type": "input_data",
        "description": "grants as the funder publishes them",
        "connector": {"kind": "fetch", "params": {"url": url, "format": "csv"}},
        "signature": {
            "form": "replaces",
            "reads": [],
            "produces": [{"name": "id", "type": "str", "nullable": False},
                         {"name": "amount", "type": "int", "nullable": False}],
        },
    }


# ─── what the model refuses before a run is ever started ─────────────────────

def test_fetch_connector_requires_a_url() -> None:
    with pytest.raises(ValueError, match="params.url is required"):
        Connector(kind=ConnectorKind.fetch, params={"format": "csv"})


def test_fetch_connector_refuses_a_non_http_url() -> None:
    with pytest.raises(ValueError, match="must be http"):
        Connector(kind=ConnectorKind.fetch, params={"url": "ftp://example.org/x.csv"})


def test_fetch_connector_requires_a_format_when_the_url_suffix_says_nothing() -> None:
    # The EA Funds shape: a path with no extension that answers csv.
    with pytest.raises(ValueError, match="params.format is required"):
        Connector(kind=ConnectorKind.fetch,
                  params={"url": "https://funds.effectivealtruism.org/api/grants"})


def test_fetch_connector_takes_a_format_free_url_whose_suffix_resolves() -> None:
    connector = Connector(kind=ConnectorKind.fetch,
                          params={"url": "https://example.org/grants.csv"})
    assert connector.params["url"].endswith("grants.csv")


def test_a_file_connector_still_needs_no_url() -> None:
    assert Connector(kind=ConnectorKind.file, params={}).params == {}


# ─── what the run service does with one ──────────────────────────────────────

def test_fetch_downloads_the_bytes_the_url_serves(served: tuple[str, Path]) -> None:
    base, root = served
    (root / "grants.csv").write_text(_GRANTS_CSV, encoding="utf-8")
    path = resolve_fetched_path(f"{base}/grants.csv")
    assert path.read_text(encoding="utf-8") == _GRANTS_CSV


def test_a_second_read_holds_the_first_copy(served: tuple[str, Path]) -> None:
    """What makes a re-run mean what the first run meant."""
    base, root = served
    (root / "grants.csv").write_text(_GRANTS_CSV, encoding="utf-8")
    url = f"{base}/grants.csv"
    resolve_fetched_path(url)
    (root / "grants.csv").write_text(_OTHER_CSV, encoding="utf-8")
    assert resolve_fetched_path(url).read_text(encoding="utf-8") == _GRANTS_CSV


def test_refetch_takes_todays_copy_without_destroying_the_first(
        served: tuple[str, Path]) -> None:
    """Two fetches are two records, so the bytes an earlier run read stay readable."""
    base, root = served
    (root / "grants.csv").write_text(_GRANTS_CSV, encoding="utf-8")
    url = f"{base}/grants.csv"
    first = resolve_fetched_path(url)
    (root / "grants.csv").write_text(_OTHER_CSV, encoding="utf-8")
    fresh = resolve_fetched_path(url, refetch=True)
    assert fresh.read_text(encoding="utf-8") == _OTHER_CSV
    assert fresh != first
    assert first.read_text(encoding="utf-8") == _GRANTS_CSV


def test_a_fetched_record_carries_the_url_it_came_from(served: tuple[str, Path]) -> None:
    """What makes "have we fetched this" a lookup rather than a path that happens to exist."""
    base, root = served
    (root / "grants.csv").write_text(_GRANTS_CSV, encoding="utf-8")
    url = f"{base}/grants.csv"
    resolve_fetched_path(url)
    record = find_fetched_file(url)
    assert record is not None
    assert record.source_url == url
    assert record.filename == "grants.csv"


def test_an_uploaded_file_is_not_a_fetched_one(served: tuple[str, Path]) -> None:
    base, root = served
    (root / "grants.csv").write_text(_GRANTS_CSV, encoding="utf-8")
    resolve_fetched_path(f"{base}/grants.csv")
    assert find_fetched_file(f"{base}/absent-from-the-store.csv") is None


def test_the_same_bytes_from_two_urls_are_two_records(served: tuple[str, Path]) -> None:
    """De-duplication is gone by design: each copy carries its own provenance."""
    base, root = served
    (root / "a").mkdir()
    (root / "b").mkdir()
    (root / "a" / "grants.csv").write_text(_GRANTS_CSV, encoding="utf-8")
    (root / "b" / "grants.csv").write_text(_GRANTS_CSV, encoding="utf-8")
    resolve_fetched_path(f"{base}/a/grants.csv")
    resolve_fetched_path(f"{base}/b/grants.csv")
    records = UploadedFile.list()
    assert len(records) == 2
    assert len({record.sha256 for record in records}) == 1   # the bytes really are the same
    assert len({record.id for record in records}) == 2       # and still two copies on disk
    assert sorted(str(record.source_url) for record in records) == [
        f"{base}/a/grants.csv", f"{base}/b/grants.csv"]


def test_a_held_copy_whose_bytes_are_gone_is_fetched_again(served: tuple[str, Path]) -> None:
    """A record is not a copy. Returning its path would name a file that is not there."""
    base, root = served
    (root / "grants.csv").write_text(_GRANTS_CSV, encoding="utf-8")
    url = f"{base}/grants.csv"
    resolve_fetched_path(url).unlink()
    assert find_fetched_file(url) is None
    assert resolve_fetched_path(url).read_text(encoding="utf-8") == _GRANTS_CSV


def test_two_urls_sharing_a_filename_do_not_share_a_copy(served: tuple[str, Path]) -> None:
    """Content addressing keys on bytes, so only the URL memo keeps these apart."""
    base, root = served
    (root / "a").mkdir()
    (root / "b").mkdir()
    (root / "a" / "grants.csv").write_text(_GRANTS_CSV, encoding="utf-8")
    (root / "b" / "grants.csv").write_text(_OTHER_CSV, encoding="utf-8")
    first = resolve_fetched_path(f"{base}/a/grants.csv")
    second = resolve_fetched_path(f"{base}/b/grants.csv")
    assert first.read_text(encoding="utf-8") == _GRANTS_CSV
    assert second.read_text(encoding="utf-8") == _OTHER_CSV


def test_a_refused_url_raises_and_leaves_no_copy(served: tuple[str, Path]) -> None:
    base, _ = served
    url = f"{base}/absent.csv"
    with pytest.raises(SourceFetchError, match="404"):
        resolve_fetched_path(url)
    assert UploadedFile.list() == []


def test_an_unreachable_host_says_so() -> None:
    with pytest.raises(SourceFetchError, match="could not be reached"):
        resolve_fetched_path("http://127.0.0.1:1/grants.csv")


# ─── what the runtime resolves a stage to ────────────────────────────────────

def _stage_of(spec: dict[str, object]) -> InputDataStage:
    stage = Workflow.model_validate({"stages": [spec]}).stages[0]
    assert isinstance(stage, InputDataStage)
    return stage


def test_a_fetch_stage_resolves_to_the_downloaded_copy(served: tuple[str, Path]) -> None:
    base, root = served
    (root / "grants.csv").write_text(_GRANTS_CSV, encoding="utf-8")
    path = resolve_source_path(_stage_of(_fetch_stage("grants", f"{base}/grants.csv")))
    assert path is not None
    assert path.read_text(encoding="utf-8") == _GRANTS_CSV


def test_a_bound_path_wins_over_the_url(served: tuple[str, Path]) -> None:
    """An operator pointing a fetch stage at a hand-downloaded copy is not overridden."""
    base, root = served
    (root / "grants.csv").write_text(_GRANTS_CSV, encoding="utf-8")
    local = root / "by-hand.csv"
    local.write_text(_OTHER_CSV, encoding="utf-8")
    spec = _fetch_stage("grants", f"{base}/grants.csv")
    spec["connector"] = {"kind": "fetch",
                         "params": {"url": f"{base}/grants.csv", "format": "csv",
                                    "path": str(local)}}
    assert resolve_source_path(_stage_of(spec)) == local
    # Nothing was downloaded to satisfy a stage the operator had already answered.
    assert UploadedFile.list() == []


def test_an_unbound_file_stage_resolves_to_nothing() -> None:
    stage = _stage_of({
        "id": "uploaded",
        "type": "input_data",
        "description": "a file the operator binds",
        "connector": {"kind": "file", "params": {}},
        "signature": {"form": "replaces", "reads": [],
                      "produces": [{"name": "id", "type": "str", "nullable": False}]},
    })
    assert resolve_source_path(stage) is None


# ─── headers ─────────────────────────────────────────────────────────────────

def test_authored_headers_reach_the_server(echoing: str) -> None:
    """The CourtListener shape: an endpoint that answers only to an Authorization token."""
    path = resolve_fetched_path(f"{echoing}/api/grants.csv",
                                headers={"Authorization": "Token abc123"})
    assert "Authorization: Token abc123" in path.read_text(encoding="utf-8")


def test_a_stage_sends_the_headers_it_authored(echoing: str) -> None:
    spec = _fetch_stage("grants", f"{echoing}/api/grants.csv")
    spec["connector"] = {"kind": "fetch",
                         "params": {"url": f"{echoing}/api/grants.csv", "format": "csv",
                                    "headers": {"Authorization": "Token abc123"}}}
    path = resolve_source_path(_stage_of(spec))
    assert path is not None
    assert "Authorization: Token abc123" in path.read_text(encoding="utf-8")


def test_a_fetch_without_headers_still_says_who_is_calling(echoing: str) -> None:
    path = resolve_fetched_path(f"{echoing}/api/grants.csv")
    assert "User-Agent: carbon-paper" in path.read_text(encoding="utf-8")


def test_an_authored_header_overrides_the_default_user_agent(echoing: str) -> None:
    path = resolve_fetched_path(f"{echoing}/api/grants.csv",
                                headers={"User-Agent": "a-named-research-bot"})
    body = path.read_text(encoding="utf-8")
    assert "User-Agent: a-named-research-bot" in body
    assert "carbon-paper" not in body


@pytest.mark.parametrize("injected", ["Token a\r\nX-Admin: yes", "Token a\nX-Admin: yes"])
def test_a_line_break_in_a_header_value_is_refused(injected: str) -> None:
    """A value carrying CR or LF would append headers of its own to the request."""
    with pytest.raises(ValueError, match="line break"):
        Connector(kind=ConnectorKind.fetch,
                  params={"url": "https://example.org/grants.csv",
                          "headers": {"Authorization": injected}})


def test_a_line_break_in_a_header_name_is_refused() -> None:
    with pytest.raises(ValueError, match="line break"):
        Connector(kind=ConnectorKind.fetch,
                  params={"url": "https://example.org/grants.csv",
                          "headers": {"Authorization\r\nX-Admin": "yes"}})


def test_a_header_value_that_is_not_a_literal_string_is_refused() -> None:
    with pytest.raises(ValueError, match="must be a literal string value"):
        Connector(kind=ConnectorKind.fetch,
                  params={"url": "https://example.org/grants.csv",
                          "headers": {"X-Limit": 10}})


def test_headers_that_are_not_a_mapping_are_refused() -> None:
    with pytest.raises(ValueError, match="must be a mapping"):
        Connector(kind=ConnectorKind.fetch,
                  params={"url": "https://example.org/grants.csv",
                          "headers": ["Authorization: Token abc123"]})


def test_a_fetch_needs_no_headers() -> None:
    connector = Connector(kind=ConnectorKind.fetch,
                          params={"url": "https://example.org/grants.csv"})
    assert "headers" not in connector.params

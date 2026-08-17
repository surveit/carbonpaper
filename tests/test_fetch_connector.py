"""The fetch connector: what the model refuses at authoring time, and what the run
service does with a URL — served by a local http server, never the public internet."""
from __future__ import annotations

import threading
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Iterator

import pytest

from app.models.stages.input_data import Connector, ConnectorKind
from app.models.workflow import Workflow
from app.services.errors import SourceFetchError
from app.services.fetched_sources import bind_fetched_sources, resolve_fetched_path

# One EA Funds grant, as their /api/grants endpoint serves it.
_GRANTS_CSV = (
    "id,fund,grantee,amount,year\n"
    "rec6rcTbCtpvrkGF3,Long-Term Future Fund,Katherine (Katie) Dammer,10000,2026\n"
)
_OTHER_CSV = "id,fund,grantee,amount,year\nrec9,Animal Welfare Fund,Someone,1,2026\n"

_PROJECT = "ai-money"


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture
def served(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    """A real http server, so the fetch runs over a socket rather than against a mock."""
    root = tmp_path / "served"
    root.mkdir()
    server = HTTPServer(("127.0.0.1", 0), partial(_QuietHandler, directory=str(root)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", root
    finally:
        server.shutdown()
        server.server_close()


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
    path = resolve_fetched_path(f"{base}/grants.csv", _PROJECT)
    assert path.read_text(encoding="utf-8") == _GRANTS_CSV


def test_a_second_read_holds_the_first_copy(served: tuple[str, Path]) -> None:
    """What makes a re-run mean what the first run meant."""
    base, root = served
    (root / "grants.csv").write_text(_GRANTS_CSV, encoding="utf-8")
    url = f"{base}/grants.csv"
    resolve_fetched_path(url, _PROJECT)
    (root / "grants.csv").write_text(_OTHER_CSV, encoding="utf-8")
    assert resolve_fetched_path(url, _PROJECT).read_text(encoding="utf-8") == _GRANTS_CSV


def test_refetch_takes_todays_copy(served: tuple[str, Path]) -> None:
    base, root = served
    (root / "grants.csv").write_text(_GRANTS_CSV, encoding="utf-8")
    url = f"{base}/grants.csv"
    resolve_fetched_path(url, _PROJECT)
    (root / "grants.csv").write_text(_OTHER_CSV, encoding="utf-8")
    fresh = resolve_fetched_path(url, _PROJECT, refetch=True)
    assert fresh.read_text(encoding="utf-8") == _OTHER_CSV


def test_two_urls_sharing_a_filename_do_not_share_a_copy(served: tuple[str, Path]) -> None:
    """Content addressing keys on bytes, so only the URL memo keeps these apart."""
    base, root = served
    (root / "a").mkdir()
    (root / "b").mkdir()
    (root / "a" / "grants.csv").write_text(_GRANTS_CSV, encoding="utf-8")
    (root / "b" / "grants.csv").write_text(_OTHER_CSV, encoding="utf-8")
    first = resolve_fetched_path(f"{base}/a/grants.csv", _PROJECT)
    second = resolve_fetched_path(f"{base}/b/grants.csv", _PROJECT)
    assert first.read_text(encoding="utf-8") == _GRANTS_CSV
    assert second.read_text(encoding="utf-8") == _OTHER_CSV


def test_a_refused_url_raises_rather_than_binding_nothing(served: tuple[str, Path]) -> None:
    base, _ = served
    with pytest.raises(SourceFetchError, match="404"):
        resolve_fetched_path(f"{base}/absent.csv", _PROJECT)


def test_an_unreachable_host_says_so() -> None:
    with pytest.raises(SourceFetchError, match="could not be reached"):
        resolve_fetched_path("http://127.0.0.1:1/grants.csv", _PROJECT)


# ─── the binding the runner is handed ────────────────────────────────────────

def test_binding_gives_every_fetch_stage_a_local_path(served: tuple[str, Path]) -> None:
    base, root = served
    (root / "grants.csv").write_text(_GRANTS_CSV, encoding="utf-8")
    workflow = Workflow.model_validate(
        {"stages": [_fetch_stage("grants", f"{base}/grants.csv")]})
    bound = bind_fetched_sources(workflow, _PROJECT, None)
    assert Path(str(bound["grants"]["path"])).read_text(encoding="utf-8") == _GRANTS_CSV


def test_binding_leaves_a_path_the_caller_already_supplied(served: tuple[str, Path]) -> None:
    """An operator pointing a fetch stage at a hand-downloaded copy is not overridden."""
    base, root = served
    (root / "grants.csv").write_text(_GRANTS_CSV, encoding="utf-8")
    local = root / "by-hand.csv"
    local.write_text(_OTHER_CSV, encoding="utf-8")
    workflow = Workflow.model_validate(
        {"stages": [_fetch_stage("grants", f"{base}/grants.csv")]})
    bound = bind_fetched_sources(workflow, _PROJECT, {"grants": {"path": str(local)}})
    assert bound["grants"]["path"] == str(local)


def test_binding_touches_no_file_connector() -> None:
    workflow = Workflow.model_validate({"stages": [{
        "id": "uploaded",
        "type": "input_data",
        "description": "a file the operator binds",
        "connector": {"kind": "file", "params": {}},
        "signature": {"form": "replaces", "reads": [],
                      "produces": [{"name": "id", "type": "str", "nullable": False}]},
    }]})
    assert bind_fetched_sources(workflow, _PROJECT, None) == {}

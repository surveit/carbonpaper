"""The fetch connector: what the model refuses at authoring time, and what the runtime
does with a URL — served by a local http server, never the public internet."""
from __future__ import annotations

import threading
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Iterator

import pytest

from app.core.errors import SourceFetchError
from app.core.fetched_sources import fetched_path_for, resolve_fetched_path
from app.models.stages.input_data import Connector, ConnectorKind, InputDataStage
from app.models.workflow import Workflow
from app.runtime.stages.input_data import resolve_source_path

# One EA Funds grant, as their /api/grants endpoint serves it.
_GRANTS_CSV = (
    "id,fund,grantee,amount,year\n"
    "rec6rcTbCtpvrkGF3,Long-Term Future Fund,Katherine (Katie) Dammer,10000,2026\n"
)
_OTHER_CSV = "id,fund,grantee,amount,year\nrec9,Animal Welfare Fund,Someone,1,2026\n"



@pytest.fixture(autouse=True)
def fetch_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CARBON_PAPER_DB_PATH", str(tmp_path / "store" / "app.db"))


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


def test_refetch_takes_todays_copy(served: tuple[str, Path]) -> None:
    base, root = served
    (root / "grants.csv").write_text(_GRANTS_CSV, encoding="utf-8")
    url = f"{base}/grants.csv"
    resolve_fetched_path(url)
    (root / "grants.csv").write_text(_OTHER_CSV, encoding="utf-8")
    fresh = resolve_fetched_path(url, refetch=True)
    assert fresh.read_text(encoding="utf-8") == _OTHER_CSV


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
    assert not fetched_path_for(url).exists()


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
    assert not fetched_path_for(f"{base}/grants.csv").exists()


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

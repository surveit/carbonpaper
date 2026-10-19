"""courtlistener_opinions against a local server, never against CourtListener."""
from __future__ import annotations

import copy
import json
import re
import threading
from collections.abc import Iterator
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from pydantic import ValidationError

from app.core.errors import MissingInputBindingError, SourceUnavailable
from app.core.files import list_project_files
from app.core.frames import read_frame_table
from app.models.connectors import SOURCE_COLUMNS
from app.models.run_manifest import index_bound_sources
from app.models.spans import Span
from app.packs.opinions import connector
from app.packs.opinions.connector import (
    CourtListenerOpinionsParams, acquire_courtlistener_opinions,
)
from app.runtime.runner import execute_run
from app.runtime.spans import SourceTextCache, verify_span
from conftest import pinned_stages, reads_of
from pdf_fixture import write_text_pdf
from stage_seed import save_version, set_stages

_FIXTURES = Path(__file__).parent.parent / "fixtures"
# Five synthetic results: 101 twice (a cursor repeat), 103 with no stored PDF, 104 with two.
_ROWS = json.loads(
    (_FIXTURES / "courtlistener_opinion_search_rows.json").read_text(encoding="utf-8"))
_SEARCH_PATH = "/api/rest/v4/search/"
_PAGE_SIZE = 2
_CLUSTER_CLAUSE = re.compile(r"cluster_id:\(([^)]*)\)")


class _Opinions:
    """What the local server holds, and every request it was sent."""

    def __init__(self, rows: list[dict], files: dict[str, bytes]) -> None:
        self.rows = rows
        self.files = files
        self.requests: list[tuple[str, str]] = []
        self.base = ""
        # A link to answer page one's `next` with, in place of a cursor on the local search.
        self.next_page = ""

    @property
    def search_requests(self) -> int:
        return sum(urlsplit(target).path == _SEARCH_PATH for target, _agent in self.requests)

    @property
    def pdf_requests(self) -> list[str]:
        paths = [urlsplit(target).path for target, _agent in self.requests]
        return [path for path in paths if path != _SEARCH_PATH]

    def pdf_bytes(self, cluster_id: int) -> bytes:
        return self.files[_path_of(cluster_id)]

    def alter_repeat(self, *, local_path: str) -> None:
        self.rows[-1]["opinions"][0]["local_path"] = local_path

    def answer(self, target: str) -> bytes | None:
        parts = urlsplit(target)
        if parts.path == _SEARCH_PATH:
            return self._search_page(parse_qs(parts.query))
        return self.files.get(parts.path.lstrip("/"))

    def _search_page(self, sent: dict[str, list[str]]) -> bytes:
        query = sent.get("q", [""])[0]
        matched = self._find_matches(query)
        start = int(sent.get("cursor", ["0"])[0])
        more = start + _PAGE_SIZE < len(matched)
        cursor = urlencode({"cursor": start + _PAGE_SIZE, "q": query, "type": "o"})
        following = (self.next_page or f"{self.base}{_SEARCH_PATH}?{cursor}") if more else None
        return json.dumps({"count": len(matched), "next": following, "previous": None,
                           "results": matched[start:start + _PAGE_SIZE]}).encode("utf-8")

    def _find_matches(self, query: str) -> list[dict]:
        clause = _CLUSTER_CLAUSE.search(query)
        if clause is None:
            return self.rows
        wanted = {int(number) for number in re.findall(r"\d+", clause.group(1))}
        return [row for row in self.rows if row["cluster_id"] in wanted]


def _path_of(cluster_id: int) -> str:
    row = next(row for row in _ROWS if row["cluster_id"] == cluster_id)
    return row["opinions"][0]["local_path"]


def _write_pdfs(directory: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for row in _ROWS:
        for opinion in row["opinions"]:
            if opinion["local_path"] is not None:
                text = [f"First page of opinion {opinion['id']}.",
                        f"Second page of opinion {opinion['id']}."]
                written = write_text_pdf(directory / f"{opinion['id']}.pdf", text)
                files[opinion["local_path"]] = written.read_bytes()
    return files


@pytest.fixture
def opinions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Opinions]:
    served = _Opinions(copy.deepcopy(_ROWS), _write_pdfs(tmp_path))

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
    monkeypatch.setattr(connector, "COURTLISTENER_API", f"{served.base}/api/rest/v4/")
    monkeypatch.setattr(connector, "OPINION_STORAGE", f"{served.base}/")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield served
    server.shutdown()
    server.server_close()


def _raw_params(**extra: object) -> dict[str, object]:
    return {"query": "qualified immunity", "courts": ["ca5", "ca9"],
            "filed_after": "2017-01-01", "filed_before": "2019-12-31",
            "statuses": ["Published", "Unpublished"], "cluster_ids": [101], **extra}


def _params(**extra: object) -> CourtListenerOpinionsParams:
    return CourtListenerOpinionsParams.model_validate(_raw_params(**extra))


# the connector against the local server

def test_each_requested_cluster_is_read_once_with_its_metadata(opinions: _Opinions) -> None:
    acquired = list(acquire_courtlistener_opinions(_params(cluster_ids=[102, 101])))
    assert [f.filename for f in acquired] == ["ca5_cluster102.pdf", "ca5_cluster101.pdf"]
    first = acquired[1]
    assert first.origin_url == f"{connector.OPINION_STORAGE}{_path_of(101)}"
    assert first.metadata["cluster_id"] == 101 and isinstance(first.metadata["date_filed"], date)
    with first.open_bytes() as stream:
        assert stream.read() == opinions.pdf_bytes(101)


def test_a_cluster_is_described_as_the_search_listed_it(opinions: _Opinions) -> None:
    by_cluster = {f.metadata["cluster_id"]: f.metadata for f in
                  acquire_courtlistener_opinions(_params(cluster_ids=[101, 102]))}
    listed = _ROWS[0]
    assert by_cluster[101] == {
        "cluster_id": 101, "opinion_id": listed["opinions"][0]["id"],
        "court_id": listed["court_id"], "case_name": listed["caseName"],
        "date_filed": date.fromisoformat(listed["dateFiled"]),
        "precedential_status": listed["status"], "docket_number": listed["docketNumber"],
        "nature_of_suit": listed["suitNature"],
        "courtlistener_url": f"https://www.courtlistener.com{listed['absolute_url']}"}
    assert by_cluster[102]["courtlistener_url"] is None
    assert by_cluster[102]["nature_of_suit"] is None


def test_the_search_is_read_across_its_pages(opinions: _Opinions) -> None:
    # Three results at two a page: 101's repeat is on page two, and it is still read.
    list(acquire_courtlistener_opinions(_params(cluster_ids=[101, 102])))
    assert opinions.search_requests == 2


def test_the_search_carries_each_param_and_every_request_names_the_app(
    opinions: _Opinions,
) -> None:
    for acquired in acquire_courtlistener_opinions(_params(cluster_ids=[102])):
        acquired.open_bytes().close()
    raw = _raw_params()
    sent = parse_qs(urlsplit(opinions.requests[0][0]).query)
    assert str(raw["query"]) in sent.pop("q")[0]
    assert sent == {
        "type": ["o"], "court": ["ca5 ca9"],
        "filed_after": [raw["filed_after"]], "filed_before": [raw["filed_before"]],
        "stat_Published": ["on"], "stat_Unpublished": ["on"]}
    assert {agent for _target, agent in opinions.requests} == {connector.USER_AGENT}


def test_every_search_page_is_narrowed_to_the_requested_clusters(opinions: _Opinions) -> None:
    list(acquire_courtlistener_opinions(_params(cluster_ids=[102, 101])))
    queries = [parse_qs(urlsplit(target).query)["q"][0] for target, _agent in opinions.requests
               if urlsplit(target).path == _SEARCH_PATH]
    assert len(queries) == 2
    for query in queries:
        clause = _CLUSTER_CLAUSE.search(query)
        assert clause is not None
        assert {int(number) for number in re.findall(r"\d+", clause.group(1))} == {101, 102}


def test_only_the_requested_pdfs_are_fetched_once_each(opinions: _Opinions) -> None:
    for acquired in acquire_courtlistener_opinions(_params(cluster_ids=[101, 102])):
        acquired.open_bytes().close()
    assert opinions.pdf_requests == [f"/{_path_of(101)}", f"/{_path_of(102)}"]


# what the connector refuses rather than guesses

def test_missing_or_unstorable_clusters_refuse_before_any_pdf_is_fetched(
    opinions: _Opinions,
) -> None:
    with pytest.raises(SourceUnavailable) as refused:
        list(acquire_courtlistener_opinions(_params(cluster_ids=[101, 103, 104, 999])))
    for cluster_id in (103, 104, 999):
        assert str(cluster_id) in str(refused.value)
    assert opinions.pdf_requests == []


def test_a_cluster_repeated_with_a_different_pdf_refuses(opinions: _Opinions) -> None:
    opinions.alter_repeat(local_path="pdf/other.pdf")  # the repeat is on page two
    with pytest.raises(SourceUnavailable, match="101"):
        list(acquire_courtlistener_opinions(_params(cluster_ids=[101, 102])))


def test_a_next_link_off_the_api_is_not_followed(opinions: _Opinions) -> None:
    # The off-API target is on the local server, so following it would show in its requests.
    opinions.next_page = f"{opinions.base}/elsewhere{_SEARCH_PATH}?cursor=2"
    with pytest.raises(SourceUnavailable, match="elsewhere.*not followed"):
        list(acquire_courtlistener_opinions(_params(cluster_ids=[101, 102])))
    assert len(opinions.requests) == 1


def test_a_result_missing_a_field_is_refused_naming_the_page_and_the_field(
    opinions: _Opinions,
) -> None:
    del opinions.rows[0]["court_id"]
    with pytest.raises(SourceUnavailable) as refused:
        list(acquire_courtlistener_opinions(_params(cluster_ids=[101])))
    assert f"{opinions.base}{_SEARCH_PATH}" in str(refused.value)
    assert "court_id" in str(refused.value)


@pytest.mark.parametrize("link", ["https://elsewhere.example/opinion/101/", "elsewhere.example/101", ""])
def test_a_page_link_that_is_not_a_path_on_the_site_refuses_before_any_pdf_is_fetched(
    opinions: _Opinions, link: str,
) -> None:
    opinions.rows[0]["absolute_url"] = link
    with pytest.raises(SourceUnavailable, match=rf"cluster 101.*{re.escape(repr(link))}"):
        list(acquire_courtlistener_opinions(_params(cluster_ids=[101, 102])))
    assert opinions.pdf_requests == []


def test_a_search_that_fails_is_reported_with_its_url(
    opinions: _Opinions, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(connector, "COURTLISTENER_API", f"{opinions.base}/api/rest/v9/")
    with pytest.raises(SourceUnavailable, match=re.escape(f"{opinions.base}/api/rest/v9/search/")):
        list(acquire_courtlistener_opinions(_params()))


def test_the_params_refuse_reversed_dates_and_repeated_clusters() -> None:
    with pytest.raises(ValidationError, match="2019-01-01.*2017-01-01"):
        _params(filed_after=date(2019, 1, 1), filed_before=date(2017, 1, 1))
    with pytest.raises(ValidationError, match="more than once"):
        _params(cluster_ids=[101, 101])


@pytest.mark.parametrize(("overrides", "refusal"), [
    ({"cluster_ids": []}, "at least 1 item"),
    ({"cluster_ids": [0]}, "greater than 0"),
    ({"courts": ["CA5"]}, "should match pattern"),
    ({"courts": []}, "at least 1 item"),
    ({"statuses": ["Reported"]}, "Published"),
    ({"statuses": []}, "at least 1 item"),
    ({"query": ""}, "at least 1 character"),
])
def test_the_params_refuse_a_value_the_search_cannot_use(
    overrides: dict[str, object], refusal: str,
) -> None:
    with pytest.raises(ValidationError, match=refusal):
        _params(**overrides)


# a run over the connector

_METADATA = [column.model_dump(mode="json", exclude_defaults=True)
             for column in connector.COURTLISTENER_OPINIONS.metadata_columns]
_SOURCE = [column.model_dump(mode="json", exclude_defaults=True) for column in SOURCE_COLUMNS]
_PAGES = [{"name": "page", "type": "int", "nullable": False},
          {"name": "page_text", "type": "str", "nullable": False},
          {"name": "page_span", "type": "span", "nullable": False}]
_READ = [column for column in _SOURCE if column["name"] in ("source_id", "source_sha256")]
_CARRIED = [column for column in _METADATA if column["name"] == "cluster_id"]


def _run_opinions(tmp_path: Path, params: dict[str, object]) -> dict:
    set_stages(tmp_path, [
        {"id": "opinions", "description": "Read the opinions", "type": "input_data",
         "row_type_id": "opinion",
         "connector": {"kind": "courtlistener_opinions", "params": params},
         "signature": {"form": "replaces", "produces": [*_SOURCE, *_METADATA]}},
        {"id": "opinion_pages", "description": "Read each opinion a page at a time",
         "type": "read_pages", "inputs": [{"id": "opinions"}], "row_type_id": "opinion_page",
         "signature": {"form": "replaces", "reads": reads_of("opinions", [*_READ, *_CARRIED]),
                       "produces": [*_READ, *_CARRIED, *_PAGES]},
         "read_pages": {"carry": ["cluster_id"]}},
    ])
    save_version(tmp_path.name, message="seed")
    return execute_run(tmp_path / "runs", tmp_path.name, *pinned_stages(tmp_path))


def _read_output(tmp_path: Path, manifest: dict, stage_id: str) -> list[dict]:
    return read_frame_table(
        tmp_path / "runs" / manifest["run_id"] / "outputs" / f"{stage_id}.parquet").to_pylist()


def test_a_run_stores_each_opinion_and_reads_its_pages_into_verified_spans(
    opinions: _Opinions, tmp_path: Path,
) -> None:
    manifest = _run_opinions(tmp_path, _raw_params(cluster_ids=[101, 102]))

    assert manifest["status"] == "ok"
    stored = {record.filename: record.origin_url for record in list_project_files(tmp_path.name)}
    assert stored == {"ca5_cluster101.pdf": f"{opinions.base}/{_path_of(101)}",
                      "ca5_cluster102.pdf": f"{opinions.base}/{_path_of(102)}"}
    rows = _read_output(tmp_path, manifest, "opinions")
    assert list(rows[0]) == [column["name"] for column in [*_SOURCE, *_METADATA]]
    assert [(row["cluster_id"], row["court_id"], row["case_name"], row["date_filed"])
            for row in rows] == [
        (listed["cluster_id"], listed["court_id"], listed["caseName"],
         date.fromisoformat(listed["dateFiled"])) for listed in _ROWS[:2]]
    pages = _read_output(tmp_path, manifest, "opinion_pages")
    assert [(page["cluster_id"], page["page"]) for page in pages] == [
        (101, 1), (101, 2), (102, 1), (102, 2)]
    bound = index_bound_sources(manifest["input_bindings"])
    texts = SourceTextCache()
    for page in pages:
        verify_span(Span.model_validate(page["page_span"]), bound, texts)


def test_a_run_naming_an_unstorable_cluster_is_refused_with_nothing_stored(
    opinions: _Opinions, tmp_path: Path,
) -> None:
    with pytest.raises(MissingInputBindingError, match="103"):
        _run_opinions(tmp_path, _raw_params(cluster_ids=[101, 103]))
    assert opinions.pdf_requests == []
    assert list_project_files(tmp_path.name) == []
